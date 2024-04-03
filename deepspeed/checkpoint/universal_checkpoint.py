# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: Apache-2.0

# DeepSpeed Team

import os
import re
import torch
import types
from .constants import (FP32_WEIGHT_KEY, PARAM, VOCAB_TENSOR, CAT_DIM, PARAM_N_SUB_PARAMS)

from deepspeed.utils import logger
from deepspeed.utils.tensor_fragment import map_to_flat_opt_states
from deepspeed.runtime import ZeROOptimizer
from deepspeed.runtime.utils import bwc_tensor_model_parallel_rank


def load_hp_checkpoint_state(self, folder, tp_rank, tp_world_size):
    hp_mapping = self._hp_mapping
    hp_mapping.optim_fragment = {}

    hp_keys = []
    for file in os.listdir(folder):
        # We expect files named something like "exp_avg.pt", "exp_avg_sq.pt", "fp32.pt"
        pattern = r'(.+).pt'
        match = re.search(pattern, file)
        if match:
            hp_keys.append(match.group(1))

    step = None
    for key in hp_keys:
        ckpt_file = os.path.join(folder, f"{key}.pt")
        ckpt_dict = torch.load(ckpt_file)

        if key == "step":
            step = ckpt_dict
            continue

        full_hp_param = ckpt_dict[PARAM]

        # need to deal with slices that were averaged.
        # the opposite of averaging here becomes an exact copy of the first slice
        # I thought of 2 ways:
        # implementation a. find a way for a client to pass a dict with patterns
        # if any(re.search(pattern, folder) for pattern in WEIGHTS_TO_AVERAGE_PATTERNS):
        #     tp_rank = 0
        #     tp_world_size = 1
        # the other approach is to assume that the saved data is correct and if full_hp_param.shape ==
        # self.shape that means we automatically copy?
        # implementation b.
        # this version requires no additional data passed from the client
        # if the shapes already match it must be slices that were averaged - so we just hack around those
        if full_hp_param.shape == self.shape:
            tp_rank = 0
            tp_world_size = 1

        # special case for word_embeddings weights which get padded differently depending on TP degree.
        # the converter to universal currently strips the original padding completely so the saved
        # weight is padding-free and we just need to add new padding depending on the target TP
        # degree
        is_vocab_tensor = ckpt_dict.get(VOCAB_TENSOR, False)
        if is_vocab_tensor:
            # In the absence of data passed from the user wrt new padded vocab specific to tp degree
            # we can again derive that data by reverse engineering the target shapes like so:
            padded_target_vocab_size = self.shape[0] * tp_world_size
            assert padded_target_vocab_size >= full_hp_param.shape[0], \
                f'Vocab tensor padded size {padded_target_vocab_size} < loaded universal size {full_hp_param.shape[0]}'
            if padded_target_vocab_size > full_hp_param.shape[0]:
                padding_size = padded_target_vocab_size - full_hp_param.shape[0]
                full_hp_param = torch.nn.functional.pad(full_hp_param, (0, 0, 0, padding_size), "constant", 0)

        full_param_numel = full_hp_param.numel()
        tp_slice_numel = self.numel()
        #        if key == FP32_WEIGHT_KEY and 'word_embeddings.weight' in folder:
        #            print_rank_0(f'{full_hp_param[:10]=}', force=True)


        assert full_param_numel == tp_world_size * tp_slice_numel, \
            f'Loading {ckpt_file} full param numel {full_param_numel} != tensor slice numel {tp_slice_numel} * tp_world_size {tp_world_size}'

        #        print(f"{full_hp_param.shape=} {full_param_numel=} {folder=}")
        #        print(f"{dst_tensor.shape=} {dst_tensor.numel()=}{folder=}")

        # since when we do many to 1 on tp we cat sometimes on dim=0 and other times on dim=1 we have to do exactly the same in reverse
        # special case is when a single parameter is effectively a container for multiple sub parameters
        # (more details at PARAM_N_SUB_PARAMS definition)
        chunk_dim = ckpt_dict.get(CAT_DIM, 0)
        n_sub_params = ckpt_dict.get(PARAM_N_SUB_PARAMS, 1)

        # get values from env var
        num_experts = int(os.getenv("NUM_EXPERTS", 16))
        hidden_size = int(os.getenv("HIDDEN_SIZE", 4096))
        n_head = int(os.getenv("N_HEAD", 32))
        n_head_kv = int(os.getenv("N_HEAD_KV", 8))
        head_dim = int(os.getenv("HEAD_DIM", 128))

        print(f"Converting param or optimizer states in {folder}. num_experts={num_experts} hidden_size={hidden_size} n_head={n_head} n_head_kv={n_head_kv} head_dim={head_dim}")

        if n_sub_params > 1:
            sub_params = full_hp_param.chunk(n_sub_params, dim=chunk_dim)
            sub_params_tp_slice = [p.chunk(tp_world_size, dim=chunk_dim)[tp_rank] for p in sub_params]
            tp_hp_slice = torch.cat(sub_params_tp_slice, dim=chunk_dim)
        else:
            # this performs the opposite of cat when merging TP slices
            if "mlp.fc1.weight" in folder:
                print(f"Reshaping mlp.fc1.weight to [{num_experts}, 2, -1, {hidden_size}] ([expert, act+swiglu_gate, hidden2, hidden1])")
                # [expert, act+swiglu_gate, hidden2, hidden1]
                tp_hp_slice = full_hp_param.view(num_experts, 2, -1, hidden_size).chunk(tp_world_size, 2)[tp_rank]
            elif "mlp.fc2.weight" in folder:
                print(f"Reshaping mlp.fc2.weight to [{num_experts}, -1, {hidden_size}] ([expert, hidden2, hidden1])")
                tp_hp_slice = full_hp_param.view(num_experts, -1, hidden_size).chunk(tp_world_size, 1)[tp_rank]
            elif "attn.Wqkv.weight" in folder:
                print(f"Reshaping attn.Wqkv.weight to [{(n_head+n_head_kv*2)*head_dim}, {hidden_size}] ([(n_head+n_head_kv*2)*head_dim, hidden])")
                full_hp_param = full_hp_param.view((n_head+n_head_kv*2)*head_dim, hidden_size)
                wq = full_hp_param[:n_head * head_dim].chunk(tp_world_size, 0)[tp_rank]
                wk = full_hp_param[n_head * head_dim:(n_head + n_head_kv) * head_dim].chunk(tp_world_size, 0)[tp_rank]
                wv = full_hp_param[(n_head + n_head_kv) * head_dim:].chunk(tp_world_size, 0)[tp_rank]
                tp_hp_slice = torch.cat([wq, wk, wv], dim=0)
            elif "attn.Wqkv.bias" in folder:
                print(f"Reshaping attn.Wqkv.bias to [{(n_head+n_head_kv*2)*head_dim}]")
                bq = full_hp_param[:n_head * head_dim].chunk(tp_world_size, 0)[tp_rank]
                bk = full_hp_param[n_head * head_dim:(n_head + n_head_kv) * head_dim].chunk(tp_world_size, 0)[tp_rank]
                bv = full_hp_param[(n_head + n_head_kv) * head_dim:].chunk(tp_world_size, 0)[tp_rank]
                tp_hp_slice = torch.cat([bq, bk, bv], dim=0)
            else:
                tp_hp_slice = full_hp_param.chunk(tp_world_size, chunk_dim)[tp_rank]

        tp_hp_slice = tp_hp_slice.flatten()

        lp_frag_address = hp_mapping.lp_fragment_address
        tp_hp_fragment = tp_hp_slice.narrow(0, lp_frag_address.start, lp_frag_address.numel)

        #        print(f"{key} SHAPE: {tp_hp_slice.shape=}")
        #        print(f"{key} SHAPE: {dst_tensor.shape=}")
        #        print(f"{key} SHAPE: {tp_hp_fragment.shape=}")

        if key == FP32_WEIGHT_KEY:
            dst_tensor = hp_mapping.get_hp_fragment()
            assert dst_tensor.numel() == lp_frag_address.numel, \
                f'Load checkpoint {key} dst numel {dst_tensor.numel()} != src numel {lp_frag_address.numel}'
            dst_tensor.data.copy_(tp_hp_fragment.data)
        else:
            assert tp_hp_fragment.numel() == lp_frag_address.numel, \
                f'Load checkpoint {key} dst numel {tp_hp_fragment.numel()} != src numel {lp_frag_address.numel}'

            hp_mapping.optim_fragment[key] = tp_hp_fragment.clone().detach()

    return step


def load_hp_checkpoint_state_from_checkpoint_dir(zero_optimizer: ZeROOptimizer, lp_groups_name: str,
                                                 checkpoint_dir: str) -> None:
    checkpoint_dir = os.path.join(checkpoint_dir, "zero")
    optim_state_path = os.path.join(checkpoint_dir, "optimizer_state.pt")
    assert os.path.isfile(
        optim_state_path), f'{optim_state_path} containing optimizer global state is missing! Cannot proceed.'
    optim_sd = torch.load(optim_state_path)

    zero_optimizer._load_global_state(optim_sd)

    tp_rank = bwc_tensor_model_parallel_rank(mpu=zero_optimizer.mpu)
    if zero_optimizer.mpu is None:
        logger.warn("MPU is not provided, setting tp size to 1 in checkpoint loading.")
        tp_world_size = 1
    else:
        tp_world_size = zero_optimizer.mpu.get_slice_parallel_world_size() if hasattr(zero_optimizer.mpu, "get_slice_parallel_world_size") \
            else zero_optimizer.mpu.get_tensor_model_parallel_world_size()

    for i, (param_group,
            loaded_param_group) in enumerate(zip(zero_optimizer.optimizer.param_groups, optim_sd['param_groups'])):
        # We have an assumption that all params in the same param_group have the same keys
        opt_keys = set()
        steps = []

        lp_groups = getattr(zero_optimizer, lp_groups_name)
        for lp in lp_groups[i]:
            if lp._hp_mapping is not None:
                #print(f"Loading {self.param_names[lp]} {tp_rank=} {tp_world_size=}")
                step = lp.load_hp_checkpoint_state(os.path.join(checkpoint_dir, zero_optimizer.param_names[lp]),
                                                   tp_rank, tp_world_size)
                for key in lp._hp_mapping.get_optim_state_keys():
                    opt_keys.add(key)
                steps.append(step)

        hp_param = param_group['params'][0]
        assert all(step == steps[0] for step in steps), f"Steps {steps} are not equal"
        if steps[0] is not None:
            zero_optimizer.optimizer.state[hp_param]['step'] = steps[0]

        map_to_flat_opt_states(hp_param, lp_groups[i], zero_optimizer.optimizer.state, opt_keys)

        for key, value in loaded_param_group.items():
            if key == 'params':
                continue
            param_group[key] = value


def enable_universal_checkpoint(param_list):
    for param in param_list:
        param.load_hp_checkpoint_state = types.MethodType(load_hp_checkpoint_state, param)

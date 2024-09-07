## Notes
- forked from https://github.com/tohtana/DeepSpeed/tree/tohtana/tp_custom_reshape on Sept 3, 2024
- change made according to https://github.com/microsoft/DeepSpeed/pull/5346/files
- renamed as sambroy/cybereo
- just adding a line here, to change the git hash -> change the version -> this is because Azure Artifacts feed will not
allow replacement after deleting a feed item (immutability considerations, i suppose).
  - hmm. that did not work. don't have time to go through setup.py to check if the githash is used, and if so, what is the githash
  - changing version.txt to 0.14.2 to bypass azure artifacts feeds filters.


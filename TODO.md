# gc-mcp TODO

- [x] unify hardware instance/dolphin instance lifecycle
    - [x] gc_hardware_list becomes part of gc_instance_list
    - [x] gc_hardware_status becomes part of gc_instance_status
    - [x] gc_hardware_remove becomes part of gc_instance_remove
    - [x] gc_instance_stop on hardware goes back to swiss
    - [x] new: gc_instance_create_emulator (create dolphin instance, don't start yet), gc_instance_create_hardware (replaces gc_hardware_register)
    - [x] gc_rom_load replaced by gc_instance_start

- [x] gc_instance_start should document how long it usually takes on hardware until the game is fully loaded
- [x] gc_instance_start should document that it will stop a game that's currently running

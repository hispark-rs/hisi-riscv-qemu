#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Integer low-latch read access profile; no inferred write/high-latch behavior."""
from ws53_gpio_test import running


def uart_config_test(qemu):
    checked=0
    for required in (False, True):
        args=('-global',f'ws53-uart.div-l-read-requires-enable={"on" if required else "off"}')
        with running(qemu,False,args) as (command,_,reset,diagnostics):
            for base in (0x52081000,0x52080000,0x57014000):
                def read(off): return int(command(f'readl {base+off:#x}')[0],0)
                def write(off,value): command(f'writel {base+off:#x} {value:#x}')
                for value in (1,17,255):
                    write(8,0xd); write(0x10,value)
                    assert read(0x10)==value
                    write(8,0xc)
                    assert read(0x10)==(0 if required else value)
                    write(8,0xd)
                    assert read(0x10)==value # Closing access does not erase it.
                    checked+=1
            reset()
            for base in (0x52081000,0x52080000,0x57014000):
                # Reset re-establishes the model's configured-console handoff.
                assert int(command(f'readl {base+0x10:#x}')[0],0)==(0 if required else 17)
                command(f'writel {base+8:#x} 0xd')
                assert int(command(f'readl {base+0x10:#x}')[0],0)==17
        assert diagnostics==[''],diagnostics
    return dict(cases=checked,uart2_hardware_read_condition_observed=True,
                other_instances_hardware_verified=False,high_latch_gating_verified=False,
                write_gating_verified=False,physical_baud_verified=False)

#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Host bytes traverse an independent8N1 virtual peer and real pad RX model."""
import argparse,json,subprocess,tempfile,time
from pathlib import Path
from ws53_gpio_test import running,connect
from ws53_uart_rx_pads_test import OPTIONS,NOTICE,BASES

PEER_NOTICE='ws53-uart: host8N1 peer drives fixed RX board pad; no flow control'


def uart_peer_test(qemu):
    with tempfile.TemporaryDirectory(prefix='ws53-uart-peers-') as directory:
        paths=[Path(directory)/str(i) for i in range(3)]
        extra=list(OPTIONS)+['-global','ws53-uart.assume-rx-peer=on','-global','ws53-uart.rx-peer-baud=100000']
        serial=[]
        for i,path in enumerate(paths):
            extra+=['-chardev',f'socket,id=peer{i},path={path},server=on,wait=off']
            serial+=['-serial',f'chardev:peer{i}']
        with running(qemu,False,tuple(extra),serial_options=tuple(serial)) as (cmd,irq,reset,diagnostics):
            def write(addr,value): cmd(f'writel {addr:#x} {value:#x}')
            def read(addr): return int(cmd(f'readl {addr:#x}')[0],0)
            def advance(ns):
                for _ in range(ns//1000): cmd('clock_step 1000')
            def prop(port,name): return cmd.qmp('qom-get',{'path':f'/machine/uart{port}','property':name})
            for port,(mux,config,mode) in enumerate(((0x57036010,0x57036030,2),(0x52008048,0x57036088,2),(0x57036008,0x57036028,1))):
                base=BASES[port]
                write(base+0x10,20);write(base+0x14,0);write(mux,mode)
                with connect(paths[port]) as peer:
                    def send(data):
                        peer.sendall(data);time.sleep(.01)
                    send(b'\x00\xff\x53')
                    assert read(base+0x4c)==0 and prop(port,'rx-peer-frames')==0, 'logical injection bypassed bit sampling'
                    advance(300000)
                    assert prop(port,'rx-peer-frames')==3 and prop(port,'pad-rx-frames')==3
                    assert read(base+0x4c)==3
                    assert [read(base+4) for _ in range(3)]==[0,255,0x53]
                    write(mux,0);send(b'\x44');advance(100000)
                    assert read(base+0x4c)==0 and prop(port,'rx-peer-frames')==4
                    write(mux,mode);write(config,0);send(b'\x45');advance(100000)
                    assert read(base+0x4c)==0 and prop(port,'rx-peer-frames')==5
                    write(config,4)
                    # Sender is external: target clock gating must not pause it.
                    cmd(f'set_irq_in /machine/uart{port} clock 0 0')
                    send(b'\x46');advance(100000)
                    assert read(base+0x4c)==0 and prop(port,'rx-peer-frames')==6
                    cmd(f'set_irq_in /machine/uart{port} clock 0 1')
                    send(b'\x47');advance(100000)
                    assert read(base+4)==0x47 and prop(port,'rx-peer-frames')==7
                    send(b'\x48');advance(20000)
                    cmd(f'set_irq_in /machine/uart{port} soft-reset-n 0 0')
                    advance(100000)
                    assert prop(port,'rx-peer-frames')==8
                    cmd(f'set_irq_in /machine/uart{port} soft-reset-n 0 1')
                    assert read(base+0x4c)==0
                    write(base+8,12);write(base+0x10,20);write(base+0x14,0);write(base+0x24,0x70)
                    send(b'\x49');advance(100000);assert read(base+4)==0x49
                    # Exceed the producer queue; backend backpressure must
                    # resume delivery without reordering or dropping bytes.
                    burst=bytes(range(129));send(burst)
                    received=[]
                    for _ in burst:
                        advance(100000)
                        assert read(base+0x4c)==1
                        received.append(read(base+4))
                    assert bytes(received)==burst and prop(port,'rx-peer-frames')==138
                    # Whole-machine reset also cancels a queued external frame.
                    send(b'\x11\x22\x33');advance(20000)
                    reset();advance(400000)
                    assert prop(port,'rx-peer-frames')==0 and read(base+0x4c)==0
            reset()
            assert all(prop(p,'rx-peer-frames')==0 for p in range(3))
        assert diagnostics[0].splitlines()==[NOTICE,PEER_NOTICE]*3,diagnostics
    for options,message in ((('-global','ws53-uart.assume-rx-peer=on'),'requires RX pad'),
            (('-global','ws53-uart.assume-pad-rx=on','-global','ws53-uart.assume-rx-peer=on',
              '-global','ws53-uart.rx-peer-baud=0'),'baud1200')):
        result=subprocess.run([qemu,'-M','ws53','-S','-display','none','-monitor','none',
                               '-serial','null',*options],capture_output=True,timeout=5)
        assert result.returncode!=0 and message in result.stderr.decode()
    return dict(passed=True,peer_ports=3,serialized_bytes=414,queue_backpressure_preserves_order=True,
                whole_machine_reset_cancels_queue=True,wrong_mux_and_input_rejected=True,
                independent_of_target_clock_reset=True,invalid_properties_rejected=True,physical_timing_qualified=False)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--qemu',required=True)
    print(json.dumps(uart_peer_test(p.parse_args().qemu),indent=2))

#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Status read-clear is not FIFO overflow or an implicit IRQ acknowledgement.

SDK documents status read-clear, but does not prove cross-register IRQ latch
coupling. Keeping IRQ pending until explicit clear is a conservative model
policy. These injected tests do not qualify real collisions or slave TXE.
"""
import argparse
import json
from ws53_gpio_test import running
from ws53_spi_test import Bus, PROFILE, NOTICE


def run(qemu):
    cases=[]
    with running(qemu,False,PROFILE) as (cmd,irq,reset,diagnostics):
        cmd('irq_intercept_in /machine/intc')
        for bus in (0,1):
            error=64 if bus else 1
            idle=6 if bus else 0x1800
            for order in ('status-first','irq-first','all-irq-first','raw-first','mask-change'):
                reset();b=Bus(cmd,bus);b.init()
                b.write('mask',b.bits(32));b.fault('collision')
                b.write('fifo',0x96);b.step(1000);b.fault('collision',False)
                assert b.frames()==1 and b.read('rx_level')==0
                assert b.read('raw')&b.bits(32) and irq.get(b.irq,False)
                if order=='irq-first':
                    assert b.read('clear_multi')==1 and not irq.get(b.irq,False)
                if order=='all-irq-first':
                    assert b.read('clear')==1 and not irq.get(b.irq,False)
                if order=='raw-first':
                    assert b.read('raw')&b.bits(32)
                    assert b.read('masked')&b.bits(32)
                if order=='mask-change':
                    b.write('mask',0);assert not irq.get(b.irq,False)
                    b.write('mask',b.bits(32));assert irq.get(b.irq,False)
                first=b.read('status');second=b.read('status')
                assert first==idle|error and second==idle
                if order not in ('irq-first','all-irq-first'):
                    assert b.read('raw')&b.bits(32) and irq.get(b.irq,False)
                    assert b.read('clear_multi')==1 and not irq.get(b.irq,False)
                assert not b.read('raw')&b.bits(32)
                cases.append(dict(bus=bus,case=order,first=first,second=second,passed=True))
            reset();b=Bus(cmd,bus);b.init();b.fault('collision')
            for n in range(2):
                b.write('fifo',n);b.step(1000)
                assert b.read('status')==idle|error and b.read('status')==idle
                assert b.read('raw')&b.bits(32)
            b.fault('collision',False);b.read('clear_multi')
            cases.append(dict(bus=bus,case='new-event-rearms-status-with-irq-still-pending',passed=True))
            reset();b=Bus(cmd,bus);b.init();b.fault('collision')
            b.write('fifo',0xa5);b.step(1000)
            reset();b=Bus(cmd,bus);b.init()
            assert b.read('status')==idle and not b.read('raw')&b.bits(32)
            # Reset clears internal latches, not a still-asserted external
            # collision-injection line. Release the peer before the next case.
            b.fault('collision',False)
            cases.append(dict(bus=bus,case='model-reset-clears-both-latches',passed=True))
            # Fill a paused master FIFO, then overflow it. This is the raw TX
            # overflow cause, not a collision or slave transmission-empty flag.
            reset();b=Bus(cmd,bus);b.init();b.fault('stretch')
            for word in range(9):b.write('fifo',word)
            assert b.read('tx_level')==8 and b.read('raw')&b.bits(2)
            b.write('mask',b.bits(2));assert irq.get(b.irq,False)
            value=b.read('status')
            assert not value&(error|(32 if bus else 0))
            assert b.read('raw')&b.bits(2) and irq.get(b.irq,False)
            assert b.read('clear_tx')==1 and not irq.get(b.irq,False)
            b.fault('stretch',False);b.step(10000)
            value=b.read('status')
            assert not value&(error|(32 if bus else 0))
            assert b.read('rx_level')==8 and value&(16 if bus else 32)
            assert b.read('status')==value  # Live FIFO status is not read-clear.
            cases.append(dict(bus=bus,case='master-fifo-overflow-is-not-status-collision-or-slave-txe',passed=True))
    assert diagnostics==[NOTICE+'\n'],diagnostics
    return dict(passed=True,cases=cases,hardware_accessed=False,status_read_clear_source='SDK comments',
        independent_irq_latches='conservative model policy; silicon relationship unverified',
        collision_silicon_verified=False,slave_txe_generated=False)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu',required=True)
    print(json.dumps(run(parser.parse_args().qemu),indent=2))

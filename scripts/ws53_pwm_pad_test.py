#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Source-backed PWM0P routing; precise digital timing, no analog claim."""
import argparse,json,subprocess
from ws53_gpio_test import running
from ws53_pwm_test import access,PROFILE,NOTICE

def pwm_pad_test(qemu, hz=2000000):
    for invalid in (0,1,32000001,18446744073709551615):
        rejected=subprocess.run([str(qemu),'-M','ws53','-display','none','-serial','none',
            '-S','-global',f'ws53-pwm.counter-hz={invalid}'],capture_output=True,timeout=3)
        assert rejected.returncode!=0 and b'counter-hz must select' in rejected.stderr
    routes=[(47,0x52008054,1),(10,0x52008028,4),(34,0x57036008,2)]
    with running(qemu,False,(*PROFILE,'-global',f'ws53-pwm.counter-hz={hz}')) as (command,level,reset,diagnostics):
        command('irq_intercept_out /machine/pinctrl level')
        read,write,clock,configure,commit,step=access(command)
        for pad,mux,mode in routes: command(f'writel {mux:#x} {mode}')
        clock(); configure(0,period=200,duty=50,offset=0); commit(0,1)
        assert all(level.get(pad,False) for pad,_,_ in routes)
        high_ns=(50*1_000_000_000+hz-1)//hz
        period_ns=200*1_000_000_000//hz
        step(high_ns-1); assert all(level[pad] for pad,_,_ in routes)
        step(1); assert all(not level[pad] for pad,_,_ in routes)
        step(period_ns-high_ns); assert all(level[pad] for pad,_,_ in routes)
        for high in (100,150):
            configure(0,period=200,duty=high,offset=0);commit(0,1)
            step((high*1_000_000_000+hz-1)//hz-1); assert level[47]
            step(2); assert not level[47]
        configure(0,period=200,duty=0,offset=0);commit(0,1);step(period_ns*10)
        assert all(not level[pad] for pad,_,_ in routes)
        configure(0,period=200,duty=200,offset=0);commit(0,1);step(period_ns*10)
        assert all(level[pad] for pad,_,_ in routes)
        command('writel 0x5703a178 0') # Input buffer does not gate PWM output.
        assert level[47]
        command('writel 0x52008054 0')
        assert not level[47] and level[10] and level[34]
        command('writel 0x52008054 1')
        assert level[47]
        write(0x100,0);commit(0,1);step(period_ns*10)
        assert all(not level[pad] for pad,_,_ in routes)
        reset()
        assert all(int(command(f'readl {mux:#x}')[0],0)==0 for _,mux,_ in routes)
    assert diagnostics[0].splitlines()==[NOTICE.replace('32000000',str(hz))],diagnostics
    return dict(routes=routes,counter_hz=hz,period_ns=period_ns,duties=[.25,.5,.75,0,1],
        rejected_clock_profiles=4,
        checks=['real PWM channel to pad','all documented PWM0P aliases','input buffer independence',
                'GPIO mux disconnect','stop low','reset mux'],hardware_verified=False)



def pwm_divider_test(qemu):
    profile=(*PROFILE,'-global','ws53-pwm.counter-hz=2000000')
    periods=[]
    with running(qemu,False,profile) as (command,level,reset,diagnostics):
        command('irq_intercept_out /machine/pinctrl level')
        command('writel 0x52008054 1')
        read,write,clock,configure,commit,step=access(command)
        clock()
        for divisor in (16,8,4,2,1):
            write(0x100,0);commit(0,1)
            command(f'writew 0x5200007c {(divisor<<2)|1}')
            command(f'writew 0x5200007c {(divisor<<2)|3}')
            assert int(command('readw 0x5200007c')[0],0)==(divisor<<2)|3
            configure(0,period=200,duty=50,offset=0);commit(0,1)
            period=200*divisor*1_000_000_000//32_000_000
            high=(50*divisor*1_000_000_000+31_999_999)//32_000_000
            assert level[47]
            step(high-1); assert level[47]
            step(1); assert not level[47]
            step(period-high); assert level[47]
            periods.append(period)
        write(0x100,0);commit(0,1);step(100000)
        assert not level[47]
        # Keep the established explicit load-edge model. A plain field update
        # without LOAD must not change the active divisor; silicon latch timing
        # outside the reviewed sequence is not claimed by this qtest.
        command('writew 0x5200007c 0x41');command('writew 0x5200007c 0x43')
        command('writew 0x5200007c 0x21')
        configure(0,period=200,duty=50,offset=0);commit(0,1)
        step(24999); assert level[47]
        step(1); assert not level[47]
        write(0x100,0);commit(0,1)
        for unsupported in (0,3,31):
            command(f'writew 0x5200007c {(unsupported<<2)|1}')
            command(f'writew 0x5200007c {(unsupported<<2)|3}')
            # Failed LOAD must retain the preceding written field and LOAD=0.
            assert int(command('readw 0x5200007c')[0],0)==(unsupported<<2)|1
    lines=diagnostics[0].splitlines()
    assert lines[0]==NOTICE.replace('32000000','2000000'),diagnostics
    assert len(lines)==4 and all(line.startswith('ws53-pwm: unimplemented divider write') for line in lines[1:]),diagnostics
    return dict(dividers=[16,8,4,2,1],period_ns=periods,high_ticks=50,period_ticks=200,
                assumed_input_hz=32000000,unsupported_divisors_rejected=[0,3,31],
                hardware_source_identity_proven=False)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--qemu',required=True)
    qemu=p.parse_args().qemu
    print(json.dumps(dict(pads=[pwm_pad_test(qemu,hz) for hz in (2000000,32000000)],
                          divider=pwm_divider_test(qemu)),indent=2))

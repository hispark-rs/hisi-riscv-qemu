/* WS53 acore MCU_MODE_1 RAM takeover profile.
 * Evidence: fbb_ws53 163d9150, acore/platform_core.h, timer_porting.c,
 * hal_timer_v150_regs_op.h. No flash boot or control-core emulation.
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#include "qemu/osdep.h"
#include "qemu/error-report.h"
#include "qemu/log.h"
#include "qemu/timer.h"
#include "qapi/error.h"
#include "hw/boards.h"
#include "hw/loader.h"
#include "hw/sysbus.h"
#include "hw/irq.h"
#include "hw/qdev-properties.h"
#include "hw/qdev-properties-system.h"
#include "chardev/char.h"
#include "chardev/char-fe.h"
#include "system/system.h"
#include "system/reset.h"
#include "exec/address-spaces.h"
#include "elf.h"
#include "hisi_riscv31.h"
#include "ws53_uart.c.inc"
#include "ws53_gpio.c.inc"
#include "ws53_pinctrl.c.inc"
#include "ws53_nmi.c.inc"
#include "ws53_wdt.c.inc"
#include "ws53_tcxo.c.inc"
#include "ws53_rtc.c.inc"
#include "ws53_pwm.c.inc"
#include "ws53_i2c.c.inc"
#include "ws53_mclken1.c.inc"
#include "ws53_spi.c.inc"
#include "ws53_tsensor.c.inc"
#include "ws53_trng.c.inc"
#include "ws53_efuse.c.inc"
#include "ws53_i2s.c.inc"
#include "ws53_mclken0.c.inc"
#include "ws53_adc.c.inc"
#include "ws53_global_clken.c.inc"
#include "ws53_mdma.c.inc"

#define TYPE_WS53_MACHINE MACHINE_TYPE_NAME("ws53")
OBJECT_DECLARE_SIMPLE_TYPE(WS53MachineState, WS53_MACHINE)

/* The SDK selects two acore timers, unlike the WS63 model's four-channel
 * compatibility window. Timer frequency is the SDK boot profile's 32 MHz;
 * clock-control registers are deliberately unmapped until modeled. */
#define WS53_TIMER_HZ 32000000ULL
typedef struct WS53TimerChannel {
    QEMUTimer *timer;
    qemu_irq irq;
    uint32_t load, control, latched;
    bool pending;
    int64_t start;
} WS53TimerChannel;

struct WS53MachineState {
    MachineState parent_obj;
    RISCVCPU cpu;
    MemoryRegion itcm, dtcm, timer_mmio;
    WS53TimerChannel timers[2];
    bool gpio_direct;
};

static void ws53_timer_irq(WS53TimerChannel *c)
{
    qemu_set_irq(c->irq, c->pending && !(c->control & 8));
}

static void ws53_timer_arm(WS53TimerChannel *c)
{
    c->start = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    uint64_t ticks = MAX(c->load, 1);
    timer_mod(c->timer, c->start + (ticks * 1000000000ULL +
                                  WS53_TIMER_HZ - 1) / WS53_TIMER_HZ);
}

static void ws53_timer_expire(void *opaque)
{
    WS53TimerChannel *c = opaque;
    c->pending = true;
    unsigned mode = (c->control >> 1) & 3;
    if ((c->control & 1) && (mode == 1 || mode == 3)) {
        ws53_timer_arm(c);
    } else {
        c->control &= ~1u;
    }
    ws53_timer_irq(c);
}

static uint32_t ws53_timer_count(WS53TimerChannel *c)
{
    if (!(c->control & 1)) {
        return 0;
    }
    uint64_t elapsed = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) - c->start;
    uint64_t ticks = elapsed * WS53_TIMER_HZ / 1000000000ULL;
    return ticks >= c->load ? 0 : c->load - ticks;
}

static uint64_t ws53_timer_read(void *opaque, hwaddr off, unsigned size)
{
    WS53MachineState *s = opaque;
    /* The SDK's common-register structure disagrees with its comments.
     * No common window or EOI read semantics are claimed for this profile. */
    if (off >= 0x100 && off < 0x220) {
        unsigned i = (off >> 8) - 1;
        WS53TimerChannel *c = &s->timers[i];
        switch (off & 0xff) {
        case 0x00: return c->load;
        case 0x08: return c->latched;
        case 0x10: return c->control;
        case 0x18: return c->pending;
        case 0x1c: return c->pending && !(c->control & 8);
        }
    }
    qemu_log_mask(LOG_GUEST_ERROR, "ws53: unimplemented TIMER read +0x%" HWADDR_PRIx "\n", off);
    return 0;
}

static void ws53_timer_write(void *opaque, hwaddr off, uint64_t val, unsigned size)
{
    WS53MachineState *s = opaque;
    if (off >= 0x100 && off < 0x220) {
        WS53TimerChannel *c = &s->timers[(off >> 8) - 1];
        switch (off & 0xff) {
        case 0x00: c->load = val; return;
        case 0x14:
            if (val & 1) { c->pending = false; }
            ws53_timer_irq(c);
            return;
        case 0x10: {
            uint32_t old = c->control;
            c->control = val & 0x2f; /* cnt_lock is hardware-owned, rstfsm a pulse */
            if (val & 0x10) {
                timer_del(c->timer);
                c->pending = false;
                c->latched = 0;
                c->control &= ~1u;
            } else if (!(val & 1)) {
                timer_del(c->timer);
            } else if (!(old & 1)) {
                ws53_timer_arm(c);
            }
            if (val & 0x20) {
                c->latched = ws53_timer_count(c);
                c->control |= 0x40;
            }
            ws53_timer_irq(c);
            return;
        }
        }
    }
    qemu_log_mask(LOG_GUEST_ERROR, "ws53: unimplemented TIMER write +0x%" HWADDR_PRIx "\n", off);
}

static const MemoryRegionOps ws53_timer_ops = {
    .read = ws53_timer_read,
    .write = ws53_timer_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 4, .max_access_size = 4 },
    .impl = { .min_access_size = 4, .max_access_size = 4 },
};

static void ws53_reset(void *opaque)
{
    WS53MachineState *s = opaque;
    cpu_reset(CPU(&s->cpu));
    for (unsigned i = 0; i < 2; i++) {
        WS53TimerChannel *c = &s->timers[i];
        timer_del(c->timer);
        /* Deterministic model initialization, not measured silicon resets. */
        c->load = c->control = c->latched = 0;
        c->pending = false;
        ws53_timer_irq(c);
    }
}

static void ws53_init(MachineState *machine)
{
    WS53MachineState *s = WS53_MACHINE(machine);
    MemoryRegion *sys = get_system_memory();
    uint64_t entry = 0x28000;
    ws63_register_custom_csrs();
    ws63_make_ram(sys, &s->itcm, "ws53.acore.itcm", 0x20000, 0x10000);
    ws63_make_ram(sys, &s->dtcm, "ws53.acore.dtcm", 0x20010000, 0x50000);
    /* No ROM, flash, shared-memory or catch-all unimplemented-device regions. */
    DeviceState *intc = qdev_new(TYPE_WS63_INTC);
    object_property_add_child(OBJECT(machine), "intc", OBJECT(intc));
    sysbus_realize_and_unref(SYS_BUS_DEVICE(intc), &error_fatal);
    DeviceState *tsensor = qdev_new(TYPE_WS53_TSENSOR);
    object_property_add_child(OBJECT(machine), "tsensor", OBJECT(tsensor));
    sysbus_realize_and_unref(SYS_BUS_DEVICE(tsensor), &error_fatal);
    sysbus_mmio_map(SYS_BUS_DEVICE(tsensor), 0, 0x59006300);
    DeviceState *trng = qdev_new(TYPE_WS53_TRNG);
    object_property_add_child(OBJECT(machine), "trng", OBJECT(trng));
    sysbus_realize_and_unref(SYS_BUS_DEVICE(trng), &error_fatal);
    sysbus_mmio_map(SYS_BUS_DEVICE(trng), 0, 0x52024000);
    sysbus_mmio_map(SYS_BUS_DEVICE(trng), 1, 0x52001600);
    DeviceState *efuse = qdev_new(TYPE_WS53_EFUSE);
    object_property_add_child(OBJECT(machine), "efuse", OBJECT(efuse));
    sysbus_realize_and_unref(SYS_BUS_DEVICE(efuse), &error_fatal);
    sysbus_mmio_map(SYS_BUS_DEVICE(efuse), 0, 0x57024000);
    sysbus_mmio_map(SYS_BUS_DEVICE(efuse), 1, 0x57030258);
    DeviceState *i2c = qdev_new(TYPE_WS53_I2C);
    object_property_add_child(OBJECT(machine), "i2c", OBJECT(i2c));
    sysbus_realize_and_unref(SYS_BUS_DEVICE(i2c), &error_fatal);
    sysbus_mmio_map(SYS_BUS_DEVICE(i2c), 0, 0x52083000);
    sysbus_mmio_map(SYS_BUS_DEVICE(i2c), 1, 0x52084000);
    sysbus_connect_irq(SYS_BUS_DEVICE(i2c), 0, qdev_get_gpio_in(intc, 62));
    sysbus_connect_irq(SYS_BUS_DEVICE(i2c), 1, qdev_get_gpio_in(intc, 63));
    DeviceState *spi = qdev_new(TYPE_WS53_SPI);
    object_property_add_child(OBJECT(machine), "spi", OBJECT(spi));
    sysbus_realize_and_unref(SYS_BUS_DEVICE(spi), &error_fatal);
    sysbus_mmio_map(SYS_BUS_DEVICE(spi), 0, 0x52087000);
    sysbus_mmio_map(SYS_BUS_DEVICE(spi), 1, 0x5208d000);
    const hwaddr spi_gates[] = {0x5200055c, 0x52000950, 0x52000574, 0x52001240};
    for (unsigned i = 0; i < ARRAY_SIZE(spi_gates); i++) {
        sysbus_mmio_map(SYS_BUS_DEVICE(spi), 2 + i, spi_gates[i]);
    }
    sysbus_connect_irq(SYS_BUS_DEVICE(spi), 0, qdev_get_gpio_in(intc, 65));
    sysbus_connect_irq(SYS_BUS_DEVICE(spi), 1, qdev_get_gpio_in(intc, 43));
    DeviceState *mclken1 = qdev_new(TYPE_WS53_MCLKEN1);
    object_property_add_child(OBJECT(machine), "mclken1", OBJECT(mclken1));
    sysbus_realize_and_unref(SYS_BUS_DEVICE(mclken1), &error_fatal);
    sysbus_mmio_map(SYS_BUS_DEVICE(mclken1), 0, 0x52000044);
    for (unsigned i = 0; i < 2; i++) {
        qdev_connect_gpio_out_named(mclken1, "gate", i, qdev_get_gpio_in_named(i2c, "gate", i));
    }
    qdev_connect_gpio_out_named(mclken1, "gate", 2, qdev_get_gpio_in_named(spi, "gate", 0));
    DeviceState *adc = qdev_new(TYPE_WS53_ADC);
    object_property_add_child(OBJECT(machine), "adc", OBJECT(adc));
    sysbus_realize_and_unref(SYS_BUS_DEVICE(adc), &error_fatal);
    sysbus_mmio_map(SYS_BUS_DEVICE(adc), 0, 0x5208a000);
    sysbus_mmio_map(SYS_BUS_DEVICE(adc), 1, 0x52000054);
    sysbus_mmio_map(SYS_BUS_DEVICE(adc), 2, 0x52004100);
    DeviceState *mdma = qdev_new(TYPE_WS53_MDMA);
    object_property_add_child(OBJECT(machine), "mdma", OBJECT(mdma));
    sysbus_realize_and_unref(SYS_BUS_DEVICE(mdma), &error_fatal);
    sysbus_mmio_map(SYS_BUS_DEVICE(mdma), 0, 0x52070000);
    sysbus_connect_irq(SYS_BUS_DEVICE(mdma), 0, qdev_get_gpio_in(intc, 58));
    ws53_mdma_connect_spi(WS53_MDMA(mdma), spi, 0, 6, 0x5208702c);
    ws53_mdma_connect_spi(WS53_MDMA(mdma), spi, 1, 4, 0x5208d060);
    ws53_mdma_connect_i2c(WS53_MDMA(mdma), i2c, 0, 0x5208301c);
    ws53_mdma_connect_i2c(WS53_MDMA(mdma), i2c, 1, 0x5208401c);
    qdev_connect_gpio_out_named(mclken1, "gate", 3, qdev_get_gpio_in_named(adc, "gate", 0));
    DeviceState *global_clken = qdev_new(TYPE_WS53_GLOBAL_CLKEN);
    object_property_add_child(OBJECT(machine), "global-clken", OBJECT(global_clken));
    sysbus_realize_and_unref(SYS_BUS_DEVICE(global_clken), &error_fatal);
    sysbus_mmio_map(SYS_BUS_DEVICE(global_clken), 0, 0x52000548);
    qdev_connect_gpio_out_named(global_clken, "gate", 0, qdev_get_gpio_in_named(spi, "global-gate", 0));
    qdev_connect_gpio_out_named(global_clken, "gate", 1, qdev_get_gpio_in_named(adc, "gate", 1));
    for (unsigned i = 0; i < 2; i++) {
        DeviceState *rtc = qdev_new(TYPE_WS53_RTC);
        g_autofree char *name = g_strdup_printf("rtc%u", i);
        object_property_add_child(OBJECT(machine), name, OBJECT(rtc));
        sysbus_realize_and_unref(SYS_BUS_DEVICE(rtc), &error_fatal);
        sysbus_mmio_map(SYS_BUS_DEVICE(rtc), 0, 0x57029100 + 0x100 * i);
        sysbus_connect_irq(SYS_BUS_DEVICE(rtc), 0, qdev_get_gpio_in(intc, 49 + i));
    }
    for (unsigned i = 0; i < 2; i++) {
        s->timers[i].irq = qdev_get_gpio_in(intc, 53 + i);
        s->timers[i].timer = timer_new_ns(QEMU_CLOCK_VIRTUAL,
                                         ws53_timer_expire, &s->timers[i]);
    }
    memory_region_init_io(&s->timer_mmio, OBJECT(machine), &ws53_timer_ops, s,
                          "ws53.timer", 0x1000);
    memory_region_add_subregion(sys, 0x52002000, &s->timer_mmio);
    const hwaddr bases[] = { 0x52081000, 0x52080000, 0x57014000 };
    const unsigned irqs[] = { 39, 41, 42 };
    for (unsigned i = 0; i < 3; i++) {
        DeviceState *uart = qdev_new(TYPE_WS53_UART);
        g_autofree char *name = g_strdup_printf("uart%u", i);
        object_property_add_child(OBJECT(machine), name, OBJECT(uart));
        qdev_prop_set_chr(uart, "chardev", serial_hd(i));
        if (i < 2 && WS53_MDMA(mdma)->assume_uart) {
            qdev_prop_set_bit(uart, "assume-dma-profile", true);
        }
        sysbus_realize_and_unref(SYS_BUS_DEVICE(uart), &error_fatal);
        sysbus_mmio_map(SYS_BUS_DEVICE(uart), 0, bases[i]);
        sysbus_connect_irq(SYS_BUS_DEVICE(uart), 0, qdev_get_gpio_in(intc, irqs[i]));
        if (i < 2) { ws53_mdma_connect_uart(WS53_MDMA(mdma), uart, i == 0 ? 10 : 8, bases[i] + 4); }
    }
    const hwaddr gpio_bases[] = { 0x5700c000, 0x57028000, 0x57010000 };
    DeviceState *nmi = qdev_new(TYPE_WS53_NMI);
    object_property_add_child(OBJECT(machine), "nmi", OBJECT(nmi));
    sysbus_realize_and_unref(SYS_BUS_DEVICE(nmi), &error_fatal);
    sysbus_mmio_map(SYS_BUS_DEVICE(nmi), 0, 0x52000700);
    DeviceState *wdt = qdev_new(TYPE_WS53_WDT);
    object_property_add_child(OBJECT(machine), "wdt", OBJECT(wdt));
    sysbus_realize_and_unref(SYS_BUS_DEVICE(wdt), &error_fatal);
    sysbus_mmio_map(SYS_BUS_DEVICE(wdt), 0, 0x5702b000);
    sysbus_connect_irq(SYS_BUS_DEVICE(wdt), 0,
                       qdev_get_gpio_in_named(nmi, "source", 1));
    DeviceState *tcxo = qdev_new(TYPE_WS53_TCXO);
    object_property_add_child(OBJECT(machine), "tcxo", OBJECT(tcxo));
    sysbus_realize_and_unref(SYS_BUS_DEVICE(tcxo), &error_fatal);
    sysbus_mmio_map(SYS_BUS_DEVICE(tcxo), 0, 0x57000200);
    DeviceState *pwm = qdev_new(TYPE_WS53_PWM);
    object_property_add_child(OBJECT(machine), "pwm", OBJECT(pwm));
    sysbus_realize_and_unref(SYS_BUS_DEVICE(pwm), &error_fatal);
    sysbus_mmio_map(SYS_BUS_DEVICE(pwm), 0, 0x52082000);
    sysbus_mmio_map(SYS_BUS_DEVICE(pwm), 1, 0x5200007c);
    sysbus_connect_irq(SYS_BUS_DEVICE(pwm), 0, qdev_get_gpio_in(intc, 71));
    sysbus_connect_irq(SYS_BUS_DEVICE(pwm), 1, qdev_get_gpio_in(intc, 72));

    DeviceState *i2s = qdev_new(TYPE_WS53_I2S);
    object_property_add_child(OBJECT(machine), "i2s", OBJECT(i2s));
    sysbus_realize_and_unref(SYS_BUS_DEVICE(i2s), &error_fatal);
    sysbus_mmio_map(SYS_BUS_DEVICE(i2s), 0, 0x52030000);
    sysbus_mmio_map(SYS_BUS_DEVICE(i2s), 1, 0x57008600);
    sysbus_connect_irq(SYS_BUS_DEVICE(i2s), 0, qdev_get_gpio_in(intc, 67));
    ws53_mdma_connect_i2s(WS53_MDMA(mdma), i2s);

    DeviceState *mclken0 = qdev_new(TYPE_WS53_MCLKEN0);
    object_property_add_child(OBJECT(machine), "mclken0", OBJECT(mclken0));
    sysbus_realize_and_unref(SYS_BUS_DEVICE(mclken0), &error_fatal);
    sysbus_mmio_map(SYS_BUS_DEVICE(mclken0), 0, 0x52000040);
    qdev_connect_gpio_out_named(mclken0, "gate", 0, qdev_get_gpio_in_named(pwm, "gate", 0));
    qdev_connect_gpio_out_named(mclken0, "gate", 1, qdev_get_gpio_in_named(i2s, "gate", 0));
    /* Do not route NMI through the ordinary local interrupt controller. CPU
     * NMI semantics are pending; the device diagnoses any asserted output. */
    DeviceState *pinctrl = qdev_new(TYPE_WS53_PINCTRL);
    object_property_add_child(OBJECT(machine), "pinctrl", OBJECT(pinctrl));
    sysbus_realize_and_unref(SYS_BUS_DEVICE(pinctrl), &error_fatal);
    qdev_connect_gpio_out_named(pwm, "out", 0,
        qdev_get_gpio_in_named(pinctrl, "pwm0-level", 0));
    qdev_connect_gpio_out_named(pwm, "output-enable", 0,
        qdev_get_gpio_in_named(pinctrl, "pwm0-drive", 0));
    const hwaddr pin_bases[] = { 0x52008000, 0x57036000, 0x5703a000 };
    for (unsigned i = 0; i < 3; i++) {
        sysbus_mmio_map(SYS_BUS_DEVICE(pinctrl), i, pin_bases[i]);
    }
    const unsigned gpio_irqs[] = { 34, 33, 29 };
    const unsigned gpio_widths[] = { 23, 12, 3 };
    for (unsigned i = 0; i < 3; i++) {
        DeviceState *gpio = qdev_new(TYPE_WS53_GPIO);
        g_autofree char *name = g_strdup_printf("gpio%u", i);
        object_property_add_child(OBJECT(machine), name, OBJECT(gpio));
        qdev_prop_set_uint32(gpio, "width", gpio_widths[i]);
        sysbus_realize_and_unref(SYS_BUS_DEVICE(gpio), &error_fatal);
        sysbus_mmio_map(SYS_BUS_DEVICE(gpio), 0, gpio_bases[i]);
        sysbus_connect_irq(SYS_BUS_DEVICE(gpio), 0, qdev_get_gpio_in(intc, gpio_irqs[i]));
        if (!s->gpio_direct) {
            for (unsigned j = 0; j < ARRAY_SIZE(ws53_pad_desc); j++) {
                const WS53PadDesc *d = &ws53_pad_desc[j];
                if (d->bank != i) { continue; }
                qdev_connect_gpio_out_named(gpio, "out", d->bit,
                    qdev_get_gpio_in_named(pinctrl, "gpio-out", d->id));
                qdev_connect_gpio_out_named(gpio, "output-enable", d->bit,
                    qdev_get_gpio_in_named(pinctrl, "gpio-oe", d->id));
                qdev_connect_gpio_out_named(pinctrl, "gpio-in", d->id,
                    qdev_get_gpio_in_named(gpio, "pin", d->bit));
            }
        }
    }
    if (machine->kernel_filename &&
        load_elf(machine->kernel_filename, NULL, NULL, NULL, &entry, NULL,
                 NULL, NULL, 0, EM_RISCV, 0, 0) <= 0) {
        error_report("ws53: cannot load RAM ELF %s", machine->kernel_filename);
        exit(1);
    }
    object_initialize_child(OBJECT(machine), "cpu", &s->cpu, machine->cpu_type);
    qdev_prop_set_uint64(DEVICE(&s->cpu), "resetvec", entry);
    s->cpu.env.mhartid = 0;
    qdev_realize(DEVICE(&s->cpu), NULL, &error_fatal);
    ws63_intc_set_cpu_env(intc, &s->cpu.env);
    qemu_register_reset(ws53_reset, s);
}

static bool ws53_get_gpio_direct(Object *obj, Error **errp)
{
    return WS53_MACHINE(obj)->gpio_direct;
}
static void ws53_set_gpio_direct(Object *obj, bool value, Error **errp)
{
    WS53_MACHINE(obj)->gpio_direct = value;
}
static void ws53_class_init(ObjectClass *oc, WS63_QOM_CLASS_DATA *data)
{
    MachineClass *mc = MACHINE_CLASS(oc);
    mc->desc = "HiSilicon WS53 application core (MCU_MODE_1 RAM test profile)";
    mc->init = ws53_init;
    mc->max_cpus = 1;
    mc->default_cpu_type = TYPE_RISCV_CPU_WS63;
    mc->default_ram_size = 0;
    object_class_property_add_bool(oc, "gpio-direct", ws53_get_gpio_direct, ws53_set_gpio_direct);
    object_class_property_set_description(oc, "gpio-direct",
        "Bypass pad routing for isolated GPIO register-contract tests only");
}

static const TypeInfo ws53_type = {
    .name = TYPE_WS53_MACHINE,
    .parent = TYPE_MACHINE,
    .instance_size = sizeof(WS53MachineState),
    .class_init = ws53_class_init,
};
static void ws53_register_types(void)
{
    type_register_static(&ws53_uart_type);
    type_register_static(&ws53_gpio_type);
    type_register_static(&ws53_pinctrl_type);
    type_register_static(&ws53_nmi_type);
    type_register_static(&ws53_wdt_type);
    type_register_static(&ws53_tcxo_type);
    type_register_static(&ws53_rtc_type);
    type_register_static(&ws53_pwm_type);
    type_register_static(&ws53_i2s_type);
    type_register_static(&ws53_mclken0_type);
    type_register_static(&ws53_adc_type);
    type_register_static(&ws53_mdma_type);
    type_register_static(&ws53_global_clken_type);
    type_register_static(&ws53_i2c_type);
    type_register_static(&ws53_mclken1_type);
    type_register_static(&ws53_spi_type);
    type_register_static(&ws53_tsensor_type);
    type_register_static(&ws53_trng_type);
    type_register_static(&ws53_efuse_type);
    type_register_static(&ws53_type);
}
type_init(ws53_register_types)

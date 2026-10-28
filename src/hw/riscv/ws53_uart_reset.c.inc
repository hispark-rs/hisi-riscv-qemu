/* H1/H0 or L0 reset word. SDK 163d9150, uart_porting.c; opt-in A-board
 * observation profiles. No undocumented neighboring bits.
 * SPDX-License-Identifier: GPL-2.0-or-later */
#define TYPE_WS53_UART_RESET "ws53-uart-reset"
OBJECT_DECLARE_SIMPLE_TYPE(WS53UartResetState, WS53_UART_RESET)
struct WS53UartResetState {
    SysBusDevice parent_obj;
    MemoryRegion mmio;
    qemu_irq reset_n[2];
    uint16_t value;
    bool assume_profile, assume_l0_profile, l0_bank;
};
static bool ws53_uart_reset_enabled(WS53UartResetState *s)
{
    return s->l0_bank ? s->assume_l0_profile : s->assume_profile;
}
static unsigned ws53_uart_reset_mask(WS53UartResetState *s)
{
    return s->l0_bank ? 0x80 : 3;
}
static void ws53_uart_reset_update(WS53UartResetState *s)
{
    if (s->l0_bank) {
        qemu_set_irq(s->reset_n[0], !!(s->value & 0x80));
        return;
    }
    /* Application UART0=H1 bit1; UART1=H0 bit0. */
    qemu_set_irq(s->reset_n[0], !!(s->value & 2));
    qemu_set_irq(s->reset_n[1], !!(s->value & 1));
}
static uint64_t ws53_uart_reset_read(void *opaque, hwaddr off, unsigned size)
{
    WS53UartResetState *s = opaque;
    if (ws53_uart_reset_enabled(s) && !off && size == 2) { return s->value; }
    qemu_log_mask(LOG_GUEST_ERROR, "ws53-uart-reset: unimplemented read off=%" HWADDR_PRIx " size=%u\n", off, size);
    return 0;
}
static void ws53_uart_reset_write(void *opaque, hwaddr off, uint64_t v, unsigned size)
{
    WS53UartResetState *s = opaque;
    if (!ws53_uart_reset_enabled(s) || off || size != 2 || (v & ~ws53_uart_reset_mask(s))) {
        qemu_log_mask(LOG_GUEST_ERROR, "ws53-uart-reset: unimplemented write off=%" HWADDR_PRIx " size=%u value=%" PRIx64 "\n", off, size, v);
        return;
    }
    s->value = v;
    ws53_uart_reset_update(s);
}
static const MemoryRegionOps ws53_uart_reset_ops = {
    .read=ws53_uart_reset_read,.write=ws53_uart_reset_write,.endianness=DEVICE_LITTLE_ENDIAN,
    .valid={.min_access_size=1,.max_access_size=8,.unaligned=true},
    .impl={.min_access_size=1,.max_access_size=8,.unaligned=true},
};
static void ws53_uart_reset_word(void *opaque)
{
    WS53UartResetState *s = opaque;
    s->value = ws53_uart_reset_mask(s); /* Released in the explicit inherited RAM boot profile. */
    ws53_uart_reset_update(s);
}
static void ws53_uart_reset_realize(DeviceState *dev, Error **errp)
{
    WS53UartResetState *s = WS53_UART_RESET(dev);
    if (s->l0_bank && s->assume_l0_profile) {
        qemu_log_mask(LOG_GUEST_ERROR, "ws53-uart-reset: observed L0 profile; divisor zero and in-flight cancellation assumed\n");
    } else if (!s->l0_bank && s->assume_profile) {
        qemu_log_mask(LOG_GUEST_ERROR, "ws53-uart-reset: observed H1/H0 profile; divisor zero and in-flight cancellation assumed\n");
    }
    qemu_register_reset(ws53_uart_reset_word, s);
}
static const Property ws53_uart_reset_properties[] = {
    DEFINE_PROP_BOOL("assume-profile", WS53UartResetState, assume_profile, false),
    DEFINE_PROP_BOOL("assume-l0-profile", WS53UartResetState, assume_l0_profile, false),
    DEFINE_PROP_BOOL("l0-bank", WS53UartResetState, l0_bank, false),
};
static void ws53_uart_reset_init(Object *obj)
{
    WS53UartResetState *s = WS53_UART_RESET(obj);
    /* Cover wide accesses so they are rejected atomically, not split into a
     * valid low halfword write plus an unassigned upper access. */
    memory_region_init_io(&s->mmio,obj,&ws53_uart_reset_ops,s,TYPE_WS53_UART_RESET,8);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj),&s->mmio);
    qdev_init_gpio_out_named(DEVICE(obj),s->reset_n,"reset-n",2);
}
static void ws53_uart_reset_class_init(ObjectClass *oc, WS63_QOM_CLASS_DATA *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    dc->realize=ws53_uart_reset_realize;
    device_class_set_props(dc,ws53_uart_reset_properties);
}
static const TypeInfo ws53_uart_reset_type = {
    .name=TYPE_WS53_UART_RESET,.parent=TYPE_SYS_BUS_DEVICE,.instance_size=sizeof(WS53UartResetState),
    .instance_init=ws53_uart_reset_init,.class_init=ws53_uart_reset_class_init,
};

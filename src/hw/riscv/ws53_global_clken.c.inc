/* Shared halfword GLOBAL_CLKEN: QSPI bit7 and ADC bit12.
 * Other bits are opaque inherited state, not modeled peripheral gates.
 * SPDX-License-Identifier: GPL-2.0-or-later */
#define TYPE_WS53_GLOBAL_CLKEN "ws53-global-clken"
OBJECT_DECLARE_SIMPLE_TYPE(WS53GlobalClkenState, WS53_GLOBAL_CLKEN)
struct WS53GlobalClkenState {
    SysBusDevice parent_obj;
    MemoryRegion mmio;
    qemu_irq gate[2];
    uint16_t value, initial_value;
};
static void ws53_global_clken_update(WS53GlobalClkenState *s)
{
    qemu_set_irq(s->gate[0], !!(s->value & 0x80));
    qemu_set_irq(s->gate[1], !!(s->value & 0x1000));
}
static uint64_t ws53_global_clken_read(void *opaque, hwaddr off, unsigned size)
{
    WS53GlobalClkenState *s = opaque;
    if (!off && size == 2) { return s->value; }
    qemu_log_mask(LOG_GUEST_ERROR, "ws53-global-clken: unimplemented read size=%u\n", size); return 0;
}
static void ws53_global_clken_write(void *opaque, hwaddr off, uint64_t v, unsigned size)
{
    WS53GlobalClkenState *s = opaque;
    if (off || size != 2 || ((v ^ s->value) & ~0x1080u)) {
        qemu_log_mask(LOG_GUEST_ERROR, "ws53-global-clken: unimplemented write value=0x%" PRIx64 " size=%u\n", v, size); return;
    }
    s->value = v; ws53_global_clken_update(s);
}
static const MemoryRegionOps ws53_global_clken_ops = {
    .read=ws53_global_clken_read,.write=ws53_global_clken_write,.endianness=DEVICE_LITTLE_ENDIAN,
    .valid={.min_access_size=1,.max_access_size=8,.unaligned=true},
    .impl={.min_access_size=1,.max_access_size=8,.unaligned=true},
};
static void ws53_global_clken_reset(void *opaque)
{
    WS53GlobalClkenState *s = opaque; s->value = s->initial_value; ws53_global_clken_update(s);
}
static void ws53_global_clken_realize(DeviceState *dev, Error **errp)
{
    qemu_register_reset(ws53_global_clken_reset, WS53_GLOBAL_CLKEN(dev));
}
static const Property ws53_global_clken_properties[] = {
    DEFINE_PROP_UINT16("initial-value", WS53GlobalClkenState, initial_value, 0),
};
static void ws53_global_clken_init(Object *obj)
{
    WS53GlobalClkenState *s = WS53_GLOBAL_CLKEN(obj);
    memory_region_init_io(&s->mmio, obj, &ws53_global_clken_ops, s, TYPE_WS53_GLOBAL_CLKEN, 2);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->mmio);
    qdev_init_gpio_out_named(DEVICE(obj), s->gate, "gate", 2);
}
static void ws53_global_clken_class_init(ObjectClass *oc, WS63_QOM_CLASS_DATA *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc); dc->realize = ws53_global_clken_realize;
    device_class_set_props(dc, ws53_global_clken_properties);
}
static const TypeInfo ws53_global_clken_type = {
    .name=TYPE_WS53_GLOBAL_CLKEN,.parent=TYPE_SYS_BUS_DEVICE,.instance_size=sizeof(WS53GlobalClkenState),
    .instance_init=ws53_global_clken_init,.class_init=ws53_global_clken_class_init,
};

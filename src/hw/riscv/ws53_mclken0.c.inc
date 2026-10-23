/* Shared M_CLKEN0: PWM halfword and I2S word accesses address identical state.
 * Other bits are inherited opaque state, never writable pretend peripherals.
 * SPDX-License-Identifier: GPL-2.0-or-later */
#define TYPE_WS53_MCLKEN0 "ws53-mclken0"
OBJECT_DECLARE_SIMPLE_TYPE(WS53Mclken0State, WS53_MCLKEN0)
struct WS53Mclken0State {
    SysBusDevice parent_obj;
    MemoryRegion mmio;
    qemu_irq gate[2];
    uint32_t value, initial_value;
};
static void ws53_mclken0_update(WS53Mclken0State *s)
{
    qemu_set_irq(s->gate[0], !!(s->value & 0x80));
    qemu_set_irq(s->gate[1], !!(s->value & 4));
}
static uint64_t ws53_mclken0_read(void *opaque, hwaddr off, unsigned size)
{
    WS53Mclken0State *s = opaque;
    if (!off && (size == 2 || size == 4)) { return size == 2 ? s->value & 0xffff : s->value; }
    qemu_log_mask(LOG_GUEST_ERROR, "ws53-mclken0: unimplemented read +0x%" HWADDR_PRIx " size=%u\n", off, size);
    return 0;
}
static void ws53_mclken0_write(void *opaque, hwaddr off, uint64_t v, unsigned size)
{
    WS53Mclken0State *s = opaque;
    uint32_t next = size == 2 ? (s->value & 0xffff0000u) | (v & 0xffff) : v;
    if (off || (size != 2 && size != 4) || ((next ^ s->value) & ~0x84u)) {
        qemu_log_mask(LOG_GUEST_ERROR, "ws53-mclken0: unimplemented write +0x%" HWADDR_PRIx " value=0x%" PRIx64 " size=%u\n", off, v, size);
        return;
    }
    s->value = next; ws53_mclken0_update(s);
}
static const MemoryRegionOps ws53_mclken0_ops = {
    .read=ws53_mclken0_read,.write=ws53_mclken0_write,.endianness=DEVICE_LITTLE_ENDIAN,
    .valid={.min_access_size=1,.max_access_size=8,.unaligned=true},
    .impl={.min_access_size=1,.max_access_size=8,.unaligned=true},
};
static void ws53_mclken0_reset(void *opaque)
{
    WS53Mclken0State *s = opaque; s->value = s->initial_value; ws53_mclken0_update(s);
}
static void ws53_mclken0_realize(DeviceState *dev, Error **errp)
{
    qemu_register_reset(ws53_mclken0_reset, WS53_MCLKEN0(dev));
}
static const Property ws53_mclken0_properties[] = {
    DEFINE_PROP_UINT32("initial-value", WS53Mclken0State, initial_value, 0),
};
static void ws53_mclken0_init(Object *obj)
{
    WS53Mclken0State *s = WS53_MCLKEN0(obj);
    memory_region_init_io(&s->mmio, obj, &ws53_mclken0_ops, s, TYPE_WS53_MCLKEN0, 4);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->mmio);
    qdev_init_gpio_out_named(DEVICE(obj), s->gate, "gate", 2);
}
static void ws53_mclken0_class_init(ObjectClass *oc, WS63_QOM_CLASS_DATA *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc); dc->realize = ws53_mclken0_realize;
    device_class_set_props(dc, ws53_mclken0_properties);
}
static const TypeInfo ws53_mclken0_type = {
    .name=TYPE_WS53_MCLKEN0,.parent=TYPE_SYS_BUS_DEVICE,.instance_size=sizeof(WS53Mclken0State),
    .instance_init=ws53_mclken0_init,.class_init=ws53_mclken0_class_init,
};

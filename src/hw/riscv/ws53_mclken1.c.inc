/* Shared M_CLKEN1 provider: one address, independent I2C0/1 and SPI0 gates.
 * TIMER/UART bits are inherited from the explicit RAM boot profile.
 * SPDX-License-Identifier: GPL-2.0-or-later */
#define TYPE_WS53_MCLKEN1 "ws53-mclken1"
OBJECT_DECLARE_SIMPLE_TYPE(WS53Mclken1State, WS53_MCLKEN1)
struct WS53Mclken1State {
    SysBusDevice parent_obj;
    MemoryRegion mmio;
    qemu_irq gate[4];
    uint16_t value;
};
static void ws53_mclken1_update(WS53Mclken1State *s)
{
    qemu_set_irq(s->gate[0], !!(s->value & 0x40));
    qemu_set_irq(s->gate[1], !!(s->value & 0x80));
    qemu_set_irq(s->gate[2], !!(s->value & 0x800));
    qemu_set_irq(s->gate[3], !!(s->value & 4));
}
static uint64_t ws53_mclken1_read(void *opaque, hwaddr off, unsigned size)
{
    WS53Mclken1State *s = opaque;
    if (!off && size == 2) { return s->value; }
    qemu_log_mask(LOG_GUEST_ERROR, "ws53-mclken1: unimplemented read size=%u\n", size);
    return 0;
}
static void ws53_mclken1_write(void *opaque, hwaddr off, uint64_t v, unsigned size)
{
    WS53Mclken1State *s = opaque;
    if (off || size != 2 || ((v ^ s->value) & ~0x8c4u)) {
        qemu_log_mask(LOG_GUEST_ERROR, "ws53-mclken1: unimplemented write value=0x%" PRIx64 " size=%u\n", v, size);
        return;
    }
    s->value = v; ws53_mclken1_update(s);
}
static const MemoryRegionOps ws53_mclken1_ops = {
    .read=ws53_mclken1_read, .write=ws53_mclken1_write, .endianness=DEVICE_LITTLE_ENDIAN,
    .valid={.min_access_size=1,.max_access_size=8,.unaligned=true},
    .impl={.min_access_size=1,.max_access_size=8,.unaligned=true},
};
static void ws53_mclken1_reset(void *opaque)
{
    WS53Mclken1State *s = opaque; s->value = 0x11; ws53_mclken1_update(s);
}
static void ws53_mclken1_realize(DeviceState *dev, Error **errp)
{
    qemu_register_reset(ws53_mclken1_reset, WS53_MCLKEN1(dev));
}
static void ws53_mclken1_init(Object *obj)
{
    WS53Mclken1State *s = WS53_MCLKEN1(obj);
    memory_region_init_io(&s->mmio, obj, &ws53_mclken1_ops, s, TYPE_WS53_MCLKEN1, 2);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->mmio);
    qdev_init_gpio_out_named(DEVICE(obj), s->gate, "gate", 4);
}
static void ws53_mclken1_class_init(ObjectClass *oc, WS63_QOM_CLASS_DATA *data)
{
    DEVICE_CLASS(oc)->realize = ws53_mclken1_realize;
}
static const TypeInfo ws53_mclken1_type = {
    .name=TYPE_WS53_MCLKEN1,.parent=TYPE_SYS_BUS_DEVICE,.instance_size=sizeof(WS53Mclken1State),
    .instance_init=ws53_mclken1_init,.class_init=ws53_mclken1_class_init,
};

/* WS53 v151 eFuse READ service; SDK 163d9150 / source contract 8d50f1e.
 * Synthetic immutable bytes, explicit boot/clock inputs, one DATA word per
 * read command. No OTP programming, security provisioning or physical identity.
 * SPDX-License-Identifier: GPL-2.0-or-later */
#define TYPE_WS53_EFUSE "ws53-efuse"
OBJECT_DECLARE_SIMPLE_TYPE(WS53EfuseState, WS53_EFUSE)
enum { EFUSE_NOT_READY, EFUSE_BAD_MODE, EFUSE_BAD_TIMING, EFUSE_COMMAND_CORRUPT,
       EFUSE_FAULT_COUNT };
struct WS53EfuseState {
    SysBusDevice parent_obj;
    MemoryRegion mmio, program_denied;
    uint8_t bytes[256];
    uint32_t byte_key, mode, period, fail_after_read;
    bool assume_profile, clock_on, boot_input, fault[EFUSE_FAULT_COUNT];
    bool armed, lost_boot;
    uint64_t reads, commands, accesses;
};
static uint32_t ws53_efuse_mode(WS53EfuseState *s)
{
    return s->fault[EFUSE_BAD_MODE] ? (s->mode & 0xffff0000u) | 0x1234 : s->mode;
}
static uint32_t ws53_efuse_period(WS53EfuseState *s)
{
    return s->fault[EFUSE_BAD_TIMING] ? (s->period & 0xffffff00u) | 7 : s->period;
}
static bool ws53_efuse_ready(WS53EfuseState *s)
{
    return s->assume_profile && s->clock_on && s->boot_input &&
           !s->lost_boot && !s->fault[EFUSE_NOT_READY];
}
static bool ws53_efuse_profile(WS53EfuseState *s)
{
    return ws53_efuse_ready(s) && (ws53_efuse_mode(s) & 0xffff) == 0x5a5a &&
           (ws53_efuse_period(s) & 0xff) == 0x1f;
}
static uint64_t ws53_efuse_read(void *opaque, hwaddr off, unsigned size)
{
    WS53EfuseState *s = opaque; s->accesses++;
    if (size != 4 || (off & 3)) { goto invalid; }
    switch (off) {
    case 0x2c: return ws53_efuse_ready(s) ? 4 : 0;
    case 0x30: return ws53_efuse_mode(s);
    case 0x34: return ws53_efuse_period(s);
    default: break;
    }
    if (off >= 0x800 && off <= 0x9fc && s->armed && ws53_efuse_profile(s)) {
        unsigned byte = ((off - 0x800) / 4) * 2;
        s->armed = false; s->reads++;
        if (s->fail_after_read && s->reads == s->fail_after_read) { s->lost_boot = true; }
        /* Poison unused upper16 to catch treating this as packed 32-bit OTP. */
        return 0xdead0000u | s->bytes[byte] | ((uint32_t)s->bytes[byte + 1] << 8);
    }
invalid:
    qemu_log_mask(LOG_GUEST_ERROR,
        "ws53-efuse: unimplemented/unavailable read +0x%" HWADDR_PRIx " size=%u\n",off,size);
    return 0;
}
static void ws53_efuse_write(void *opaque, hwaddr off, uint64_t v, unsigned size)
{
    WS53EfuseState *s = opaque; s->accesses++;
    if (size != 4 || (off & 3)) { goto invalid; }
    if (off == 0x34) { s->period = v; s->armed = false; return; }
    if (off == 0x30 && (v & 0xffff) == 0x5a5a) {
        s->mode = v; s->commands++;
        if (s->fault[EFUSE_COMMAND_CORRUPT]) { s->mode = (v & 0xffff0000u) | 0x1234; }
        s->armed = ws53_efuse_profile(s);
        if (!s->assume_profile) {
            qemu_log_mask(LOG_GUEST_ERROR,"ws53-efuse: unimplemented read service without assume-profile\n");
        }
        return;
    }
invalid:
    qemu_log_mask(LOG_GUEST_ERROR,
        "ws53-efuse: rejected write +0x%" HWADDR_PRIx " value=0x%" PRIx64 " size=%u\n",off,v,size);
}
static uint64_t ws53_efuse_program_read(void *opaque, hwaddr off, unsigned size)
{
    qemu_log_mask(LOG_GUEST_ERROR,"ws53-efuse: programming switch read unsupported\n");
    return 0;
}
static void ws53_efuse_program_write(void *opaque, hwaddr off, uint64_t v, unsigned size)
{
    qemu_log_mask(LOG_GUEST_ERROR,"ws53-efuse: programming switch write rejected\n");
}
static const MemoryRegionOps ws53_efuse_ops = {
    .read=ws53_efuse_read,.write=ws53_efuse_write,.endianness=DEVICE_LITTLE_ENDIAN,
    .valid={.min_access_size=1,.max_access_size=8,.unaligned=true},
    .impl={.min_access_size=1,.max_access_size=8,.unaligned=true},
};
static const MemoryRegionOps ws53_efuse_program_ops = {
    .read=ws53_efuse_program_read,.write=ws53_efuse_program_write,.endianness=DEVICE_LITTLE_ENDIAN,
    .valid={.min_access_size=1,.max_access_size=8,.unaligned=true},
    .impl={.min_access_size=1,.max_access_size=8,.unaligned=true},
};
static void ws53_efuse_clock(void *opaque, int n, int level)
{
    WS53EfuseState *s = opaque; s->clock_on = level; s->armed = false;
}
static void ws53_efuse_boot(void *opaque, int n, int level)
{
    WS53EfuseState *s = opaque; s->boot_input = level;
    s->lost_boot = false; s->armed = false;
}
static void ws53_efuse_fault(void *opaque, int n, int level)
{
    WS53EfuseState *s = opaque; s->fault[n] = level; s->armed = false;
}
static void ws53_efuse_reset(void *opaque)
{
    WS53EfuseState *s = opaque;
    s->mode = s->period = 0; s->armed = s->lost_boot = false;
    s->reads = s->commands = s->accesses = 0;
    /* Synthetic contents and externally controlled levels/faults persist. */
}
static void ws53_efuse_realize(DeviceState *dev, Error **errp)
{
    WS53EfuseState *s = WS53_EFUSE(dev);
    if (s->byte_key > 255) { error_setg(errp,"ws53-efuse: test-byte-key must fit u8"); return; }
    for (unsigned i=0; i<256; i++) { s->bytes[i] = (i * 73 + s->byte_key) & 0xff; }
    if (s->assume_profile) {
        qemu_log_mask(LOG_GUEST_ERROR,
            "ws53-efuse: assumed immutable-test-image boot-input timing1f one-word-per-read-command; NO programming\n");
    }
    qemu_register_reset(ws53_efuse_reset,s);
}
static const Property ws53_efuse_properties[] = {
    DEFINE_PROP_BOOL("assume-profile",WS53EfuseState,assume_profile,false),
    DEFINE_PROP_UINT32("test-byte-key",WS53EfuseState,byte_key,19),
};
static void ws53_efuse_init(Object *obj)
{
    WS53EfuseState *s = WS53_EFUSE(obj); s->clock_on = true;
    memory_region_init_io(&s->mmio,obj,&ws53_efuse_ops,s,TYPE_WS53_EFUSE,0x1000);
    memory_region_init_io(&s->program_denied,obj,&ws53_efuse_program_ops,s,"ws53-efuse-program-denied",8);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj),&s->mmio);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj),&s->program_denied);
    qdev_init_gpio_in_named(DEVICE(obj),ws53_efuse_clock,"clock",1);
    qdev_init_gpio_in_named(DEVICE(obj),ws53_efuse_boot,"boot-done",1);
    qdev_init_gpio_in_named(DEVICE(obj),ws53_efuse_fault,"fault",EFUSE_FAULT_COUNT);
    object_property_add_uint32_ptr(obj,"fail-after-read",&s->fail_after_read,OBJ_PROP_FLAG_READ|OBJ_PROP_FLAG_WRITE);
    object_property_add_uint64_ptr(obj,"data-reads",&s->reads,OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj,"read-commands",&s->commands,OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj,"accesses",&s->accesses,OBJ_PROP_FLAG_READ);
}
static void ws53_efuse_class_init(ObjectClass *oc, WS63_QOM_CLASS_DATA *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc); dc->realize = ws53_efuse_realize;
    device_class_set_props(dc,ws53_efuse_properties);
}
static const TypeInfo ws53_efuse_type = {
    .name=TYPE_WS53_EFUSE,.parent=TYPE_SYS_BUS_DEVICE,.instance_size=sizeof(WS53EfuseState),
    .instance_init=ws53_efuse_init,.class_init=ws53_efuse_class_init,
};

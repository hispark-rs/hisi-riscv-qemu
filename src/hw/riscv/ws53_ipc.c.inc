/* WS53 IPC application doorbells, source/ROM contract b449799.
 * Explicit digital profile; no vendor-core firmware, shared ring, cache,
 * physical clock/reset timing, cross-core ownership or power model.
 * SPDX-License-Identifier: GPL-2.0-or-later */
#define TYPE_WS53_IPC "ws53-ipc"
OBJECT_DECLARE_SIMPLE_TYPE(WS53IpcState, WS53_IPC)
struct WS53IpcState {
    SysBusDevice parent_obj;
    MemoryRegion mmio;
    qemu_irq irq[2];
    uint32_t enable, receive, send;
    uint64_t ack_writes, sync_reads, send_writes, rejected;
    bool assume_profile, clock_on, hold_ack;
};
static void ws53_ipc_update(WS53IpcState *s)
{
    for (int i = 0; i < 2; i++) {
        qemu_set_irq(s->irq[i], s->assume_profile && s->clock_on &&
                     (s->enable & s->receive & (1u << i)));
    }
}
static MemTxResult ws53_ipc_reject(WS53IpcState *s, hwaddr off,
                                  unsigned size, bool write)
{
    s->rejected++;
    qemu_log_mask(LOG_GUEST_ERROR,
        "ws53-ipc: unavailable/invalid %s +0x%" HWADDR_PRIx " size=%u\n",
        write ? "write" : "read", off, size);
    return MEMTX_ERROR;
}
static MemTxResult ws53_ipc_read(void *opaque, hwaddr off, uint64_t *value,
                                unsigned size, MemTxAttrs attrs)
{
    WS53IpcState *s = opaque;
    *value = 0;
    if (!s->assume_profile || !s->clock_on || size != 4) {
        return ws53_ipc_reject(s, off, size, false);
    }
    switch (off) {
    case 0: *value = s->enable; break;
    case 8: *value = s->receive; break;
    case 12:
        s->sync_reads++;
        /* Deliberately unrelated to pending: the real patch discards this. */
        *value = 0xa55a5aa5;
        break;
    case 16: *value = s->send; break;
    default: return ws53_ipc_reject(s, off, size, false);
    }
    return MEMTX_OK;
}
static MemTxResult ws53_ipc_write(void *opaque, hwaddr off, uint64_t value,
                                 unsigned size, MemTxAttrs attrs)
{
    WS53IpcState *s = opaque;
    if (!s->assume_profile || !s->clock_on) {
        return ws53_ipc_reject(s, off, size, true);
    }
    if (off == 0 && size == 4 && !(value & ~0xffffu)) {
        /* ROM's full 0xffff enable is accepted; only two modeled IRQs exist. */
        s->enable = value;
    } else if (off == 12 && size == 4 && !(value & ~3u)) {
        s->ack_writes++;
        if (!s->hold_ack) { s->receive &= ~value; }
    } else if (off == 20 && size == 2 && !(value & ~3u)) {
        s->send_writes++;
        s->send |= value;
    } else if (off == 24 && size == 4 && value == 1) {
        /* The sole known SDK release command is not a reset/flush command. */
    } else {
        return ws53_ipc_reject(s, off, size, true);
    }
    ws53_ipc_update(s);
    return MEMTX_OK;
}
static const MemoryRegionOps ws53_ipc_ops = {
    .read_with_attrs = ws53_ipc_read, .write_with_attrs = ws53_ipc_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 1, .max_access_size = 8, .unaligned = true },
    .impl = { .min_access_size = 1, .max_access_size = 8, .unaligned = true },
};
static void ws53_ipc_peer(void *opaque, int n, int level)
{
    WS53IpcState *s = opaque;
    if (level && s->assume_profile && s->clock_on) {
        if (n < 2) { s->receive |= 1u << n; }
        else { s->send &= ~(1u << (n - 2)); }
    }
    ws53_ipc_update(s);
}
static void ws53_ipc_clock(void *opaque, int n, int level)
{
    WS53IpcState *s = opaque;
    s->clock_on = level;
    ws53_ipc_update(s);
}
static void ws53_ipc_fault(void *opaque, int n, int level)
{
    WS53_IPC(opaque)->hold_ack = level;
}
static void ws53_ipc_reset(void *opaque)
{
    WS53IpcState *s = opaque;
    s->enable = s->receive = s->send = 0;
    s->ack_writes = s->sync_reads = s->send_writes = s->rejected = 0;
    /* Digital deterministic reset, not a silicon reset value. Inputs persist. */
    ws53_ipc_update(s);
}
static void ws53_ipc_realize(DeviceState *dev, Error **errp)
{
    WS53IpcState *s = WS53_IPC(dev);
    if (s->assume_profile) {
        qemu_log_mask(LOG_GUEST_ERROR,
            "ws53-ipc: assumed digital two-channel doorbell/ACK/clock profile; no ring or peer ownership proof\n");
    }
    qemu_register_reset(ws53_ipc_reset, s);
}
static const Property ws53_ipc_properties[] = {
    DEFINE_PROP_BOOL("assume-profile", WS53IpcState, assume_profile, false),
};
static void ws53_ipc_init(Object *obj)
{
    WS53IpcState *s = WS53_IPC(obj);
    s->clock_on = true;
    memory_region_init_io(&s->mmio, obj, &ws53_ipc_ops, s, TYPE_WS53_IPC, 0x20);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->mmio);
    for (int i = 0; i < 2; i++) { sysbus_init_irq(SYS_BUS_DEVICE(obj), &s->irq[i]); }
    qdev_init_gpio_in_named(DEVICE(obj), ws53_ipc_peer, "peer", 4);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_ipc_clock, "clock", 1);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_ipc_fault, "fault", 1);
    object_property_add_uint64_ptr(obj, "ack-writes", &s->ack_writes, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "sync-reads", &s->sync_reads, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "send-writes", &s->send_writes, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "rejections", &s->rejected, OBJ_PROP_FLAG_READ);
}
static void ws53_ipc_class_init(ObjectClass *oc, WS63_QOM_CLASS_DATA *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    dc->realize = ws53_ipc_realize;
    device_class_set_props(dc, ws53_ipc_properties);
}
static const TypeInfo ws53_ipc_type = {
    .name = TYPE_WS53_IPC, .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(WS53IpcState), .instance_init = ws53_ipc_init,
    .class_init = ws53_ipc_class_init,
};

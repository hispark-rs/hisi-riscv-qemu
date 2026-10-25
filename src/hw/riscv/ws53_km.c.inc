/* WS53 KM clear-key loader, SDK 163d9150 / RDL 6cda2bb.
 * Explicit model assumptions: atomic REE/TEE claims, 8/2 slots, staging latched
 * on START, completion/flush after 5us, selected-bit ACK. No root derivation,
 * common route, OTP, Flash keys, physical erasure or cross-core handoff proof.
 * Model reset cancels in-flight work and clears private key storage; this is
 * not evidence of silicon reset/zeroization. Test diagnostics contain only
 * synthetic fixture keys. SPDX-License-Identifier: GPL-2.0-or-later */
#define TYPE_WS53_KM "ws53-km"
OBJECT_DECLARE_SIMPLE_TYPE(WS53KmState, WS53_KM)
enum { KM_LOAD_STALL, KM_REJECT_CLAIM, KM_LOSE_OWNER, KM_ACK_STUCK,
       KM_FLUSH_STALL, KM_FLUSH_TIMEOUT, KM_FLUSH_FAIL, KM_KEEP_OWNER,
       KM_FAULT_COUNT };
typedef struct WS53KmSlot {
    uint8_t owner, valid, engine, size, usage;
    uint8_t key[128];
} WS53KmSlot;
struct WS53KmState {
    SysBusDevice parent_obj;
    MemoryRegion mmio;
    qemu_irq irq;
    QEMUTimer *load_timer, *flush_timer;
    bool assume_profile, route_clear_irq, clock_on, fault[KM_FAULT_COUNT];
    WS53KmSlot slots[10];
    uint32_t data[4], address, config, security, control, raw;
    uint32_t command[2], flush[2], selector, loader_error, controller_error;
    uint32_t fault_after_submissions;
    uint8_t staging_mask, pending_bytes[16], pending_slot, pending_block;
    uint8_t pending_engine, pending_size, pending_usage, flush_slot, flush_owner;
    bool loading, flushing;
    uint64_t submitted, completed, released, rejections, reads, writes;
};
static void ws53_km_irq(WS53KmState *s)
{
    /* No guessed guest enable register: external routing is an opt-in model
     * assumption, distinct from the documented raw event and IRQ number. */
    qemu_set_irq(s->irq, s->assume_profile && s->route_clear_irq &&
                 s->clock_on && (s->raw & (1u << 14)));
}
static void ws53_km_reject(WS53KmState *s, const char *op, hwaddr off)
{
    s->rejections++;
    qemu_log_mask(LOG_GUEST_ERROR, "ws53-km: unimplemented/unavailable %s +0x%" HWADDR_PRIx "\n", op, off);
}
static int ws53_km_slot(uint32_t selector)
{
    unsigned number = selector & 1023;
    if (selector & (1u << 16)) { return -1; } /* TS-cipher not implemented. */
    if (selector & (1u << 15)) { return number < 2 ? 8 + number : -1; }
    return number < 8 ? number : -1;
}
static void ws53_km_schedule(WS53KmState *s)
{
    if (!s->clock_on || !s->assume_profile) { return; }
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    bool load_stalled = s->fault[KM_LOAD_STALL] && s->submitted > s->fault_after_submissions;
    if (s->loading && !load_stalled && !timer_pending(s->load_timer)) {
        timer_mod(s->load_timer, now + 5000);
    }
    if (s->flushing && !s->fault[KM_FLUSH_STALL] && !timer_pending(s->flush_timer)) {
        timer_mod(s->flush_timer, now + 5000);
    }
}
static void ws53_km_load_tick(void *opaque)
{
    WS53KmState *s = opaque;
    WS53KmSlot *slot = &s->slots[s->pending_slot];
    if (!s->loading || !s->clock_on) { return; }
    s->loading = false;
    s->control &= ~1u;
    if (s->fault[KM_LOSE_OWNER] && s->submitted > s->fault_after_submissions) { slot->owner = 2; }
    /* Errors/lost ownership leave key material and valid mask unchanged. The
     * owner read/error words expose the failed path without fabricated bits. */
    if (slot->owner == 1 && !s->loader_error && !s->controller_error) {
        memcpy(slot->key + 16 * s->pending_block, s->pending_bytes, 16);
        slot->valid |= 1u << s->pending_block;
        slot->engine = s->pending_engine;
        slot->size = s->pending_size;
        slot->usage = s->pending_usage;
        s->completed++;
        s->raw |= 1u << 14;
    }
    ws53_km_irq(s);
}
static void ws53_km_flush_tick(void *opaque)
{
    WS53KmState *s = opaque;
    WS53KmSlot *slot = &s->slots[s->flush_slot];
    if (!s->flushing || !s->clock_on) { return; }
    s->flushing = false;
    uint32_t *status = &s->flush[s->flush_owner - 1];
    *status = s->fault[KM_FLUSH_TIMEOUT] ? 4 : s->fault[KM_FLUSH_FAIL] ? 2 : 0;
    if (!*status && !s->fault[KM_KEEP_OWNER] && slot->owner == s->flush_owner &&
        !s->loader_error && !s->controller_error) {
        /* Deliberately retain bytes after unlock. No physical erasure promise. */
        slot->owner = slot->valid = 0;
        s->released++;
    }
}
static void ws53_km_submit(WS53KmState *s, uint32_t value)
{
    unsigned engine = (s->config >> 4) & 255, size = (value >> 2) & 3;
    bool hmac = engine == 0xa0 || engine == 0xa1 || engine == 0xa2;
    unsigned block = hmac ? (value >> 13) & 7 : s->address & 1;
    unsigned slot = hmac ? 8 + (s->address & 1023) : (s->address & 1023) >> 1;
    if (s->loading || s->flushing || !(value & 1) || s->staging_mask != 15 ||
        (s->config & 7) != 1 || !(s->config & 0x30000) || (s->security & 63) != 10 ||
        (hmac ? ((s->address & 1023) >= 2 || size != 0 ||
                 ((engine == 0xa0 || engine == 0xa2) && block >= 4)) :
                ((engine != 0x20 && engine != 0x50) || slot >= 8 || !size ||
                 (engine == 0x50 && size != 1))) || s->loader_error || s->controller_error ||
        s->slots[slot].owner != 1) {
        ws53_km_reject(s, "submission", 0x1438); return;
    }
    s->pending_slot = slot; s->pending_block = block;
    s->pending_engine = engine; s->pending_size = size;
    s->pending_usage = (s->config >> 16) & 3;
    for (unsigned i = 0; i < 4; i++) { stl_le_p(s->pending_bytes + 4 * i, s->data[i]); }
    s->staging_mask = 0; s->control = value; s->loading = true; s->submitted++;
    ws53_km_schedule(s);
}
static void ws53_km_slot_command(WS53KmState *s, unsigned owner, uint32_t value)
{
    int index = ws53_km_slot(value);
    if (index < 0 || s->loading || s->flushing) {
        ws53_km_reject(s, "slot-command", owner == 1 ? 0x1b04 : 0x1b00); return;
    }
    WS53KmSlot *slot = &s->slots[index];
    s->command[owner - 1] = value;
    if (value & (1u << 20)) {
        if (!slot->owner && !s->fault[KM_REJECT_CLAIM]) { slot->owner = owner; }
    } else if (slot->owner != owner) {
        s->flush[owner - 1] = 2;
    } else {
        s->flush_slot = index; s->flush_owner = owner; s->flush[owner - 1] = 1;
        s->flushing = true; ws53_km_schedule(s);
    }
}
static uint64_t ws53_km_read(void *opaque, hwaddr off, unsigned size)
{
    WS53KmState *s = opaque; s->reads++;
    if (size != 4 || (off & 3) || !s->assume_profile || !s->clock_on) { goto invalid; }
    switch (off) {
    case 0x1010: return s->address;
    case 0x1014: return s->config;
    case 0x1018: return s->security;
    case 0x1038: return s->loader_error;
    case 0x103c: return s->controller_error;
    case 0x1044: return s->raw;
    case 0x1438: return s->control;
    case 0x1b00: return s->command[1];
    case 0x1b04: return s->command[0];
    case 0x1b10: return s->flush[1];
    case 0x1b14: return s->flush[0];
    case 0x1b30: return s->selector;
    case 0x1b34: {
        int index = ws53_km_slot(s->selector);
        if (index < 0) { goto invalid; }
        return s->slots[index].owner;
    }
    default: break;
    }
invalid: ws53_km_reject(s, "read", off); return 0;
}
static void ws53_km_write(void *opaque, hwaddr off, uint64_t val, unsigned size)
{
    WS53KmState *s = opaque; uint32_t value = val; s->writes++;
    if (size != 4 || (off & 3) || !s->assume_profile || !s->clock_on) { goto invalid; }
    if (off >= 0x1000 && off <= 0x100c) {
        if (s->loading) { goto invalid; }
        unsigned i = (off - 0x1000) / 4;
        s->data[i] = value; s->staging_mask |= 1u << i; return;
    }
    switch (off) {
    case 0x1010: if (s->loading) { goto invalid; } s->address = value; return;
    case 0x1014: if (s->loading) { goto invalid; } s->config = value; return;
    case 0x1018: if (s->loading) { goto invalid; } s->security = value; return;
    case 0x1044:
        if (value & ~0x41f0u) { goto invalid; }
        if (!s->fault[KM_ACK_STUCK]) { s->raw &= ~(value & (1u << 14)); }
        ws53_km_irq(s); return;
    case 0x1438: ws53_km_submit(s, value); return;
    case 0x1b00: ws53_km_slot_command(s, 2, value); return;
    case 0x1b04: ws53_km_slot_command(s, 1, value); return;
    case 0x1b30:
        if (ws53_km_slot(value) < 0) { goto invalid; }
        s->selector = value; return;
    default: break;
    }
invalid: ws53_km_reject(s, "write", off);
}
static const MemoryRegionOps ws53_km_ops = {
    .read = ws53_km_read, .write = ws53_km_write, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 1, .max_access_size = 8, .unaligned = true },
    .impl = { .min_access_size = 1, .max_access_size = 8, .unaligned = true },
};
static void ws53_km_clock(void *opaque, int n, int level)
{
    WS53KmState *s = opaque; s->clock_on = level;
    timer_del(s->load_timer); timer_del(s->flush_timer);
    ws53_km_schedule(s); ws53_km_irq(s);
}
static void ws53_km_fault(void *opaque, int n, int level)
{
    WS53KmState *s = opaque; s->fault[n] = level;
    timer_del(s->load_timer); timer_del(s->flush_timer);
    ws53_km_schedule(s);
}
static char *ws53_km_debug_keys(Object *obj, Error **errp)
{
    WS53KmState *s = WS53_KM(obj); GString *text = g_string_new(NULL);
    for (unsigned i = 0; i < 10; i++) {
        if (i) { g_string_append_c(text, ':'); }
        for (unsigned j = 0; j < 128; j++) { g_string_append_printf(text, "%02x", s->slots[i].key[j]); }
    }
    return g_string_free(text, false);
}
static char *ws53_km_debug_slots(Object *obj, Error **errp)
{
    WS53KmState *s = WS53_KM(obj); GString *text = g_string_new(NULL);
    for (unsigned i = 0; i < 10; i++) {
        WS53KmSlot *slot = &s->slots[i];
        if (i) { g_string_append_c(text, ':'); }
        g_string_append_printf(text, "%02x%02x%02x%02x", slot->owner, slot->valid, slot->engine, slot->size);
    }
    return g_string_free(text, false);
}
static char *ws53_km_debug_staging(Object *obj, Error **errp)
{
    WS53KmState *s = WS53_KM(obj); GString *text = g_string_new(NULL);
    for (unsigned i = 0; i < 4; i++) {
        for (unsigned j = 0; j < 4; j++) { g_string_append_printf(text, "%02x", (s->data[i] >> (8 * j)) & 255); }
    }
    return g_string_free(text, false);
}
static void ws53_km_reset(void *opaque)
{
    WS53KmState *s = opaque;
    timer_del(s->load_timer); timer_del(s->flush_timer);
    memset(s->slots, 0, sizeof(s->slots)); memset(s->data, 0, sizeof(s->data));
    memset(s->pending_bytes, 0, sizeof(s->pending_bytes));
    memset(s->command, 0, sizeof(s->command)); memset(s->flush, 0, sizeof(s->flush));
    s->address = s->config = s->security = s->control = s->raw = s->selector = 0;
    s->loader_error = s->controller_error = 0; s->staging_mask = 0;
    s->loading = s->flushing = false;
    s->submitted = s->completed = s->released = s->rejections = s->reads = s->writes = 0;
    /* Keep external faults/clock as inputs, not fabricated reset values. */
    ws53_km_irq(s);
}
static void ws53_km_realize(DeviceState *dev, Error **errp)
{
    WS53KmState *s = WS53_KM(dev);
    if (s->assume_profile) {
        qemu_log_mask(LOG_GUEST_ERROR, "ws53-km: assumed slot/clear-loader profile; no silicon ownership or erasure proof\n");
    }
    qemu_register_reset(ws53_km_reset, s);
}
static const Property ws53_km_properties[] = {
    DEFINE_PROP_BOOL("assume-profile", WS53KmState, assume_profile, false),
    DEFINE_PROP_BOOL("route-clear-irq", WS53KmState, route_clear_irq, false),
};
static void ws53_km_init(Object *obj)
{
    WS53KmState *s = WS53_KM(obj); s->clock_on = true;
    memory_region_init_io(&s->mmio, obj, &ws53_km_ops, s, TYPE_WS53_KM, 0x2000);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->mmio); sysbus_init_irq(SYS_BUS_DEVICE(obj), &s->irq);
    s->load_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, ws53_km_load_tick, s);
    s->flush_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, ws53_km_flush_tick, s);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_km_clock, "clock", 1);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_km_fault, "fault", KM_FAULT_COUNT);
    object_property_add_str(obj, "debug-keys", ws53_km_debug_keys, NULL);
    object_property_add_str(obj, "debug-slots", ws53_km_debug_slots, NULL);
    object_property_add_str(obj, "debug-staging", ws53_km_debug_staging, NULL);
    object_property_add_uint32_ptr(obj, "inject-loader-error", &s->loader_error, OBJ_PROP_FLAG_READWRITE);
    object_property_add_uint32_ptr(obj, "inject-controller-error", &s->controller_error, OBJ_PROP_FLAG_READWRITE);
    object_property_add_uint32_ptr(obj, "inject-raw", &s->raw, OBJ_PROP_FLAG_READWRITE);
    object_property_add_uint32_ptr(obj, "fault-after-submissions", &s->fault_after_submissions, OBJ_PROP_FLAG_READWRITE);
    object_property_add_uint64_ptr(obj, "submitted-blocks", &s->submitted, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "completed-blocks", &s->completed, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "released-slots", &s->released, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "rejections", &s->rejections, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "reads", &s->reads, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "writes", &s->writes, OBJ_PROP_FLAG_READ);
}
static void ws53_km_finalize(Object *obj)
{
    WS53KmState *s = WS53_KM(obj); timer_free(s->load_timer); timer_free(s->flush_timer);
}
static void ws53_km_class_init(ObjectClass *oc, WS63_QOM_CLASS_DATA *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc); dc->realize = ws53_km_realize;
    device_class_set_props(dc, ws53_km_properties);
}
static const TypeInfo ws53_km_type = {
    .name = TYPE_WS53_KM, .parent = TYPE_SYS_BUS_DEVICE, .instance_size = sizeof(WS53KmState),
    .instance_init = ws53_km_init, .instance_finalize = ws53_km_finalize, .class_init = ws53_km_class_init,
};

/* Abstract CMD53 peer and length-delimited ADMA, explicitly assumed.
 * Source: f39e085 ROM request/descriptor audit. Ordinary VALID|TRAN records
 * use an 8-byte stride and 16-bit nonzero lengths. Actual silicon termination
 * is unverified: this opt-in profile stops at the command's byte count, not at
 * an invented END flag in complete_send. No CIS, CMD52, CRC or electrical model.
 * Posted writes deliberately survive source masking, clock loss and IP reset.
 * SPDX-License-Identifier: GPL-2.0-or-later */
static void ws53_sdio_adma_schedule(WS53SdioState *s)
{
    if (s->posted && !s->posted_stall && !timer_pending(s->posted_timer)) {
        timer_mod(s->posted_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) +
                  (s->delay_posted ? 100000 : 1000));
    }
    if (s->assume_adma && s->dma_active && !s->posted && !s->dma_stall &&
        ws53_sdio_accessible(s) && s->host_clock && !timer_pending(s->dma_timer)) {
        timer_mod(s->dma_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 1000);
    }
}
static bool ws53_sdio_dma_range(uint32_t address, unsigned size)
{
    return size && address >= 0x20010000 && (uint64_t)address + size <= 0x20060000;
}
static void ws53_sdio_adma_error(WS53SdioState *s)
{
    s->dma_active = false;
    s->request_pending = false;
    s->dma_errors++;
    s->regs[0x110 / 4] |= 0x80 | (s->host_write ? 0 : 4);
    ws53_sdio_update(s);
}
static void ws53_sdio_adma_advance(WS53SdioState *s, unsigned bytes)
{
    s->transferred += bytes;
    s->descriptor_address += bytes;
    s->descriptor_left -= bytes;
    s->dma_bytes += bytes;
    if (s->transferred == s->request_length) {
        s->dma_active = false;
        s->transfers++;
        if (!s->suppress_completion) {
            s->regs[0x110 / 4] |= 0x20 | (s->host_write ? 1 : 2);
        }
        ws53_sdio_update(s);
    } else {
        ws53_sdio_adma_schedule(s);
    }
}
static void ws53_sdio_adma_posted_write(void *opaque)
{
    WS53SdioState *s = opaque;
    if (s->posted_stall) {
        return; /* Explicit external bus hold, released independently of gates. */
    }
    /* The accepted transaction is a separate bus obligation. Reset clears
     * the peripheral state but cannot retract it in this adversarial profile. */
    MemTxResult result = s->write_error ? MEMTX_ERROR :
        address_space_write(&address_space_memory, s->posted_address,
                            MEMTXATTRS_UNSPECIFIED, s->posted_data, s->posted_length);
    s->posted = false;
    if (s->posted_epoch != s->epoch) {
        s->posted_after_reset++;
        return;
    }
    if (result != MEMTX_OK) {
        ws53_sdio_adma_error(s);
        return;
    }
    ws53_sdio_adma_advance(s, s->posted_length);
}
static void ws53_sdio_adma_tick(void *opaque)
{
    WS53SdioState *s = opaque;
    uint8_t descriptor[8], bytes[4];
    if (!s->dma_active || s->posted || s->dma_stall ||
        !ws53_sdio_accessible(s) || !s->host_clock) {
        return;
    }
    if (!s->descriptor_left) {
        uint64_t address = (uint64_t)s->table_pointer + s->descriptor_index * 8;
        if (s->descriptor_index >= 129 || address > UINT32_MAX ||
            !ws53_sdio_dma_range(address, 8) || s->read_error ||
            address_space_read(&address_space_memory, address, MEMTXATTRS_UNSPECIFIED,
                               descriptor, 8) != MEMTX_OK ||
            lduw_le_p(descriptor) != 0x21 || !lduw_le_p(descriptor + 2)) {
            ws53_sdio_adma_error(s);
            return;
        }
        s->descriptor_index++;
        s->descriptor_left = MIN(lduw_le_p(descriptor + 2), s->request_length - s->transferred);
        s->descriptor_address = ldl_le_p(descriptor + 4);
        if (!ws53_sdio_dma_range(s->descriptor_address, s->descriptor_left)) {
            ws53_sdio_adma_error(s);
            return;
        }
    }
    unsigned size = MIN(s->descriptor_left, 4);
    if (s->host_write) {
        memcpy(s->posted_data, s->host_data->data + s->transferred, size);
        s->posted_address = s->descriptor_address;
        s->posted_length = size;
        s->posted_epoch = s->epoch;
        s->posted = true;
        timer_mod(s->posted_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) +
                  (s->delay_posted ? 100000 : 1000));
    } else {
        if (s->read_error || address_space_read(&address_space_memory, s->descriptor_address,
            MEMTXATTRS_UNSPECIFIED, bytes, size) != MEMTX_OK) {
            ws53_sdio_adma_error(s);
            return;
        }
        g_byte_array_append(s->device_data, bytes, size);
        ws53_sdio_adma_advance(s, size);
    }
}
static void ws53_sdio_adma_request(WS53SdioState *s)
{
    uint32_t argument = s->next_command, count = argument & 0x1ff;
    uint32_t length = argument & (1u << 27) ? count * s->next_block_size : (count ? count : 512);
    bool write = argument & (1u << 31);
    if (!s->assume_adma || !ws53_sdio_configured(s) || !(s->regs[0x11c / 4] & 1) ||
        s->dma_active || s->request_pending || s->posted || (s->regs[0x110 / 4] & 0x1ff) ||
        ((argument >> 28) & 7) != 1 || (argument & 0x07fffe00) ||
        s->next_block_size > 4095 || !length || length > 0x1fffff ||
        (write && s->host_data->len != length)) {
        ws53_sdio_reject(s, true, 0x4000, 4);
        return;
    }
    s->regs[0x254 / 4] = argument;
    s->regs[0x258 / 4] = s->next_block_size;
    s->request_length = length;
    s->host_write = write;
    s->request_pending = true;
    if (!write) {
        s->offer_pending = false;
    }
    g_byte_array_set_size(s->device_data, 0);
    s->regs[0x110 / 4] |= write ? 0x10 : 8;
    ws53_sdio_update(s);
}
static void ws53_sdio_adma_submit(WS53SdioState *s, uint32_t pointer)
{
    if (!s->request_pending || s->dma_active || s->posted || !ws53_sdio_configured(s) ||
        (s->regs[0x110 / 4] & 0x1ff) || (pointer & 3) ||
        (!s->host_write && (s->regs[0x120 / 4] & 0x1fffff) != s->request_length)) {
        ws53_sdio_reject(s, true, 0x40, 4);
        return;
    }
    s->table_pointer = pointer;
    s->descriptor_index = s->descriptor_left = s->transferred = 0;
    s->request_pending = false;
    s->dma_active = true;
    s->epoch++;
    ws53_sdio_adma_schedule(s);
}
static void ws53_sdio_adma_reset(WS53SdioState *s)
{
    timer_del(s->dma_timer);
    s->request_pending = s->dma_active = false;
    s->epoch++;
    /* Keep posted data/timer/epoch until that bus transaction completes. */
    s->request_length = s->transferred = s->descriptor_index = s->descriptor_left = 0;
    s->table_pointer = s->descriptor_address = 0;
    s->offer_pending = false;
    s->offered_length = 0;
    g_byte_array_set_size(s->device_data, 0);
}
static void ws53_sdio_host_data_set(Object *obj, const char *value, Error **errp)
{
    WS53SdioState *s = WS53_SDIO(obj);
    size_t length = strlen(value);
    if (!s->assume_adma || s->request_pending || s->dma_active || s->posted ||
        (length & 1) || length / 2 > 0x1fffff) {
        error_setg(errp, "ws53-sdio: host data requires an idle ADMA profile and bounded hex bytes");
        return;
    }
    for (size_t i = 0; i < length; i++) {
        if (!g_ascii_isxdigit(value[i])) {
            error_setg(errp, "ws53-sdio: malformed hex host data");
            return;
        }
    }
    g_byte_array_set_size(s->host_data, length / 2);
    for (size_t i = 0; i < length / 2; i++) {
        s->host_data->data[i] = (g_ascii_xdigit_value(value[2*i]) << 4) | g_ascii_xdigit_value(value[2*i+1]);
    }
}
static char *ws53_sdio_device_data_get(Object *obj, Error **errp)
{
    WS53SdioState *s = WS53_SDIO(obj);
    char *result = g_malloc(s->device_data->len * 2 + 1);
    for (size_t i = 0; i < s->device_data->len; i++) {
        g_snprintf(result + 2*i, 3, "%02x", s->device_data->data[i]);
    }
    result[2*s->device_data->len] = 0;
    return result;
}
static bool ws53_sdio_dma_active_get(Object *obj, Error **errp)
{
    return WS53_SDIO(obj)->dma_active;
}
static bool ws53_sdio_posted_get(Object *obj, Error **errp)
{
    return WS53_SDIO(obj)->posted;
}
static bool ws53_sdio_offer_get(Object *obj, Error **errp)
{
    return WS53_SDIO(obj)->offer_pending;
}
static void ws53_sdio_adma_init(Object *obj)
{
    WS53SdioState *s = WS53_SDIO(obj);
    s->host_data = g_byte_array_new();
    s->device_data = g_byte_array_new();
    s->dma_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, ws53_sdio_adma_tick, s);
    s->posted_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, ws53_sdio_adma_posted_write, s);
    object_property_add_uint32_ptr(obj, "host-command", &s->next_command, OBJ_PROP_FLAG_READWRITE);
    object_property_add_uint32_ptr(obj, "host-block-size", &s->next_block_size, OBJ_PROP_FLAG_READWRITE);
    object_property_add_str(obj, "host-write-data", NULL, ws53_sdio_host_data_set);
    object_property_add_str(obj, "device-read-data", ws53_sdio_device_data_get, NULL);
    object_property_add_bool(obj, "dma-active", ws53_sdio_dma_active_get, NULL);
    object_property_add_bool(obj, "dma-posted-write", ws53_sdio_posted_get, NULL);
    object_property_add_bool(obj, "read-offer-pending", ws53_sdio_offer_get, NULL);
    object_property_add_uint32_ptr(obj, "read-offer-length", &s->offered_length, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "read-notifications", &s->read_notifications, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "dma-transfers", &s->transfers, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "dma-bytes", &s->dma_bytes, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "dma-errors", &s->dma_errors, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "dma-posted-after-reset", &s->posted_after_reset, OBJ_PROP_FLAG_READ);
}

/* WS53 application NMI aggregation, SDK 163d9150 source profile.
 * Register evidence: hisi-registers 4939cd2, non_os_nmi.c.
 * Source-driven digital levels only; CPU entry/return is NOT modeled.
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#define TYPE_WS53_NMI "ws53-nmi"
OBJECT_DECLARE_SIMPLE_TYPE(WS53NmiState, WS53_NMI)
#define WS53_NMI_SOURCES 0x3e
struct WS53NmiState {
    SysBusDevice parent_obj;
    MemoryRegion mmio;
    uint16_t enabled, raw;
    bool asserted, delivery_diagnosed;
    qemu_irq output[1];
};

static void ws53_nmi_update(WS53NmiState *s)
{
    bool level = (s->enabled & s->raw) != 0;
    if (level && !s->delivery_diagnosed) {
        /* Do not let a guest silently proceed after an undelivered NMI. One
         * diagnostic per device lifetime avoids flooding a held-source test. */
        qemu_log_mask(LOG_GUEST_ERROR,
            "ws53-nmi: unimplemented CPU NMI delivery (aggregation output only)\n");
        s->delivery_diagnosed = true;
    }
    if (level != s->asserted) {
        s->asserted = level;
        qemu_set_irq(s->output[0], level);
    }
}

static void ws53_nmi_input(void *opaque, int n, int level)
{
    WS53NmiState *s = opaque;
    if (n < 1 || n > 5) {
        qemu_log_mask(LOG_GUEST_ERROR,
            "ws53-nmi: unimplemented source %d\n", n);
        return;
    }
    uint16_t mask = 1u << n;
    s->raw = level ? s->raw | mask : s->raw & ~mask;
    ws53_nmi_update(s);
}

static uint64_t ws53_nmi_read(void *opaque, hwaddr off, unsigned size)
{
    WS53NmiState *s = opaque;
    if (size == 2) {
        switch (off) {
        case 0: return s->enabled;
        case 4: return s->raw;
        case 8: return s->raw & s->enabled;
        }
    }
    qemu_log_mask(LOG_GUEST_ERROR,
        "ws53-nmi: unimplemented read +0x%" HWADDR_PRIx " size=%u\n",
        off, size);
    return 0;
}

static void ws53_nmi_write(void *opaque, hwaddr off, uint64_t value,
                           unsigned size)
{
    WS53NmiState *s = opaque;
    if (size == 2 && off == 0 && !(value & ~WS53_NMI_SOURCES)) {
        s->enabled = value;
        ws53_nmi_update(s);
        return;
    }
    qemu_log_mask(LOG_GUEST_ERROR,
        "ws53-nmi: unimplemented write +0x%" HWADDR_PRIx
        " size=%u value=0x%" PRIx64 "\n", off, size, value);
}

static const MemoryRegionOps ws53_nmi_ops = {
    .read = ws53_nmi_read, .write = ws53_nmi_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    /* Pass byte/word/unaligned accesses through for explicit rejection and
     * diagnostics; never split a word into an accidentally valid halfword. */
    .valid = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
    .impl = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
};

static void ws53_nmi_reset(void *opaque)
{
    WS53NmiState *s = opaque;
    /* Deterministic RAM profile, not silicon reset values. External sources
     * stay asserted until their owning peripheral/test deasserts them. */
    s->enabled = 0;
    ws53_nmi_update(s);
}

static void ws53_nmi_init(Object *obj)
{
    WS53NmiState *s = WS53_NMI(obj);
    memory_region_init_io(&s->mmio, obj, &ws53_nmi_ops, s,
                         TYPE_WS53_NMI, 0x10);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->mmio);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_nmi_input, "source", 16);
    qdev_init_gpio_out_named(DEVICE(obj), s->output, "pending", 1);
    qemu_register_reset(ws53_nmi_reset, s);
}
static const TypeInfo ws53_nmi_type = {
    .name = TYPE_WS53_NMI, .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(WS53NmiState), .instance_init = ws53_nmi_init,
};

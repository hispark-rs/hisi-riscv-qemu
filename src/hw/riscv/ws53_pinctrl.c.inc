/* WS53 pad/mux digital routing. SDK 163d9150 + hisi-registers 2e7cc51.
 * This is not an analog/drive-strength/timing or silicon-reset model.
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#define TYPE_WS53_PINCTRL "ws53-pinctrl"
OBJECT_DECLARE_SIMPLE_TYPE(WS53PinctrlState, WS53_PINCTRL)
typedef struct WS53PadDesc {
    unsigned id, modes, pad_mask, bank, bit;
    hwaddr mux, pad;
} WS53PadDesc;
/* Canonical SDK IDs; bank0 holes are not physical pads. */
#define PAD_M(n, modes, mask) { n, modes, mask, 0, n, 0x52008000 + 4 * n, 0x57036040 + 4 * n }
#define PAD_A(n, modes, mask, muxoff, padoff) { 32 + n, modes, mask, 1, n, 0x57036000 + muxoff, 0x57036000 + padoff }
#define PAD_S(n, modes, pin) { 45 + n, modes, 0x3f, 2, n, 0x52008000 + 4 * pin, 0x5703a170 + 4 * n }
static const WS53PadDesc ws53_pad_desc[] = {
    PAD_M(0, 0xff, 0xff), PAD_M(2, 0x87, 0xff), PAD_M(3, 0x87, 0xff),
    PAD_M(4, 0x87, 0xff), PAD_M(5, 0x83, 0x3f), PAD_M(8, 0x9f, 0x3f),
    PAD_M(9, 0x9f, 0x3f), PAD_M(10, 0xf7, 0x3f), PAD_M(12, 0xf7, 0x3f),
    PAD_M(13, 0xbf, 0x3f), PAD_M(17, 0xef, 0x3f), PAD_M(18, 0xff, 0x3f),
    PAD_M(19, 0xef, 0x3f), PAD_M(20, 0xef, 0x3f), PAD_M(22, 0xef, 0x3f),
    PAD_A(0, 0x9f, 0x3f, 0xbc, 0x58), PAD_A(1, 0x83, 0x3f, 4, 0x24),
    PAD_A(2, 0x87, 0x3f, 8, 0x28), PAD_A(3, 0x87, 0x3f, 0xc, 0x2c),
    PAD_A(4, 0x87, 0x3f, 0x10, 0x30), PAD_A(5, 0x87, 0xff, 0x14, 0x34),
    PAD_A(6, 0xff, 0xff, 0xb8, 0x44), PAD_A(7, 0xb7, 0x3f, 0xc4, 0x6c),
    PAD_A(8, 0x87, 0x3f, 0xc8, 0x80), PAD_A(9, 0xdf, 0x3f, 0xc0, 0x5c),
    PAD_A(10, 0x83, 0x3f, 0x18, 0x38), PAD_A(11, 0x83, 0x3f, 0x1c, 0x3c),
    { 44, 0, 0x3f, 255, 0, 0, 0x57036020 }, /* reset pad only */
    PAD_S(0, 0xff, 14), PAD_S(1, 0xf7, 15), PAD_S(2, 0xff, 21),
};
#undef PAD_M
#undef PAD_A
#undef PAD_S

typedef struct WS53PinRegion {
    WS53PinctrlState *owner;
    MemoryRegion mmio;
    hwaddr base;
} WS53PinRegion;
struct WS53PinctrlState {
    SysBusDevice parent_obj;
    WS53PinRegion regions[3];
    uint8_t mode[48], config[48];
    CharBackend pad_trace;
    uint8_t traced_state[48];
    bool external_level[48], external_drive[48], gpio_out[48], gpio_oe[48];
    bool delivered[48];
    bool pwm0_level, pwm0_drive;
    bool uart_tx_profile, uart_console_profile, uart_l0_console_profile;
    bool uart_level[3], uart_drive[3];
    uint8_t uart_byte[3];
    uint64_t uart_frame_pads[3], uart_bytes[48], uart_hash[48];
    bool uart_rx_profile, uart_rx_ambiguous[3];
    int uart_rx_selected[3];
    qemu_irq uart_rx_level[3], uart_rx_enable[3];
    qemu_irq gpio_in[48], level[48], drive[48], floating[48], contention[48];
};

static int ws53_pad_uart_tx(unsigned id, unsigned mode)
{
    if (id == 12 && mode == 2) { return 0; }
    if ((id == 17 && mode == 2) || (id == 8 && mode == 1)) { return 1; }
    if (id == 33 && mode == 1) { return 2; }
    return -1;
}
static int ws53_pad_uart_rx_port(unsigned id, unsigned mode)
{
    if (id == 36 && mode == 2) { return 0; }
    if ((id == 18 && mode == 2) || (id == 9 && mode == 1)) { return 1; }
    if (id == 34 && mode == 1) { return 2; }
    return -1;
}

/* Multi-selected RX semantics are not evidenced. Fail closed instead of
 * inventing an OR, AND, priority mux or last-writer selection. */
static void ws53_pad_uart_rx_update(WS53PinctrlState *s)
{
    if (!s->uart_rx_profile) { return; }
    for (unsigned port = 0; port < 3; port++) {
        int selected = -1;
        unsigned count = 0;
        for (unsigned i = 0; i < ARRAY_SIZE(ws53_pad_desc); i++) {
            unsigned id = ws53_pad_desc[i].id;
            if ((s->config[id] & 4) && ws53_pad_uart_rx_port(id, s->mode[id]) == port) {
                selected = id; count++;
            }
        }
        bool ambiguous = count > 1;
        if (ambiguous && !s->uart_rx_ambiguous[port]) {
            qemu_log_mask(LOG_GUEST_ERROR, "ws53-pinctrl: UART%u multiple RX pads are unqualified; input disconnected\n", port);
        }
        s->uart_rx_ambiguous[port] = ambiguous;
        if (ambiguous) { selected = -1; }
        bool high = true;
        if (selected >= 0) {
            unsigned pull = (s->config[selected] >> 3) & 3;
            if (!s->external_drive[selected] && !pull) { selected = -1; }
            else { high = s->external_drive[selected] ? s->external_level[selected] : pull == 1; }
        }
        if (selected != s->uart_rx_selected[port]) {
            qemu_set_irq(s->uart_rx_enable[port], 0);
            s->uart_rx_selected[port] = selected;
        }
        qemu_set_irq(s->uart_rx_level[port], high);
        qemu_set_irq(s->uart_rx_enable[port], selected >= 0);
    }
}

/* WS53 hardware guide pin-function table: PWM0P on MGPIO10 mode4,
 * MGPIO21/SGPIO2 mode1, AGPIO2 mode2. SDK PWM sample uses pin47/mode1/ch0.
 * Other PWM channels and alternate peripheral routes remain unimplemented. */
static bool ws53_pad_pwm0(unsigned id, unsigned mode)
{
    return (id == 10 && mode == 4) || (id == 47 && mode == 1) ||
           (id == 34 && mode == 2);
}

static const WS53PadDesc *ws53_pad_id(unsigned id)
{
    for (unsigned i = 0; i < ARRAY_SIZE(ws53_pad_desc); i++) {
        if (ws53_pad_desc[i].id == id) { return &ws53_pad_desc[i]; }
    }
    return NULL;
}

static void ws53_pad_update(WS53PinctrlState *s, const WS53PadDesc *d)
{
    unsigned n = d->id, pull = (s->config[n] >> 3) & 3;
    bool gpio = d->bank != 255 && s->mode[n] == 0;
    bool pwm = ws53_pad_pwm0(n, s->mode[n]);
    int uart = s->uart_tx_profile ? ws53_pad_uart_tx(n, s->mode[n]) : -1;
    bool driving = (gpio && s->gpio_oe[n]) || (pwm && s->pwm0_drive) ||
                   (uart >= 0 && s->uart_drive[uart]);
    bool driven_level = uart >= 0 ? s->uart_level[uart] :
                        pwm ? s->pwm0_level : s->gpio_out[n];
    bool floating = !driving && !s->external_drive[n] && pull == 0;
    bool conflict = driving && s->external_drive[n] &&
                    driven_level != s->external_level[n];
    if (conflict) {
        for (unsigned port = 0; port < 3; port++) { s->uart_frame_pads[port] &= ~(1ULL << n); }
    }
    /* Deterministic digital resolution: active internal output wins a conflict,
     * which is
     * separately reported. Floating resolves low. Neither policy predicts an
     * analog voltage. Drive code/Schmitt thresholds have no electrical model. */
    bool level = driving ? driven_level :
                 s->external_drive[n] ? s->external_level[n] : pull == 1;
    bool input = gpio && (s->config[n] & 4) && level;
    uint8_t state = level | (driving << 1) | (floating << 2) | (conflict << 3);
    if (state != s->traced_state[n]) {
        s->traced_state[n] = state;
        if (qemu_chr_fe_backend_connected(&s->pad_trace)) {
            /* Digital model transitions, not voltage or electrical timing.
             * CSV: virtual_ns,SDK_pin,level,driving,floating,contention. */
            char row[128];
            int len = snprintf(row, sizeof(row), "%" PRId64 ",%u,%u,%u,%u,%u\n",
                qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL), n, level, driving, floating, conflict);
            qemu_chr_fe_write_all(&s->pad_trace, (const uint8_t *)row, len);
        }
    }
    qemu_set_irq(s->level[n], level);
    qemu_set_irq(s->drive[n], driving);
    qemu_set_irq(s->floating[n], floating);
    qemu_set_irq(s->contention[n], conflict);
    /* Cache before notifying GPIO: its output refresh feeds back into us. */
    if (s->delivered[n] != input) {
        s->delivered[n] = input;
        qemu_set_irq(s->gpio_in[n], input);
    }
    ws53_pad_uart_rx_update(s);
}

static void ws53_pad_signal(WS53PinctrlState *s, bool *values, int n, int level)
{
    const WS53PadDesc *d = n < 0 ? NULL : ws53_pad_id(n);
    if (!d) {
        qemu_log_mask(LOG_GUEST_ERROR, "ws53-pinctrl: unimplemented pad signal %d\n", n);
        return;
    }
    if (values[n] != (level != 0)) {
        values[n] = level != 0;
        ws53_pad_update(s, d);
    }
}
#define PAD_INPUT(fn, member) \
static void fn(void *opaque, int n, int level) { \
    WS53PinctrlState *s = opaque; ws53_pad_signal(s, s->member, n, level); \
}
PAD_INPUT(ws53_pad_external_level, external_level)
PAD_INPUT(ws53_pad_external_drive, external_drive)
PAD_INPUT(ws53_pad_gpio_out, gpio_out)
PAD_INPUT(ws53_pad_gpio_oe, gpio_oe)
#undef PAD_INPUT

static void ws53_pad_pwm0_update(WS53PinctrlState *s)
{
    for (unsigned i = 0; i < ARRAY_SIZE(ws53_pad_desc); i++) {
        const WS53PadDesc *d = &ws53_pad_desc[i];
        if (ws53_pad_pwm0(d->id, s->mode[d->id])) { ws53_pad_update(s, d); }
    }
}
static void ws53_pad_pwm0_level(void *opaque, int n, int level)
{
    WS53PinctrlState *s = opaque;
    s->pwm0_level = level != 0;
    ws53_pad_pwm0_update(s);
}
static void ws53_pad_pwm0_drive(void *opaque, int n, int level)
{
    WS53PinctrlState *s = opaque;
    s->pwm0_drive = level != 0;
    ws53_pad_pwm0_update(s);
}

static void ws53_pad_uart_update(WS53PinctrlState *s, int port)
{
    for (unsigned i = 0; i < ARRAY_SIZE(ws53_pad_desc); i++) {
        const WS53PadDesc *d = &ws53_pad_desc[i];
        if (ws53_pad_uart_tx(d->id, s->mode[d->id]) == port) {
            ws53_pad_update(s, d);
        }
    }
}
static void ws53_pad_uart_level(void *opaque, int n, int level)
{
    WS53PinctrlState *s = opaque;
    s->uart_level[n] = level != 0;
    ws53_pad_uart_update(s, n);
}
static void ws53_pad_uart_drive(void *opaque, int n, int level)
{
    WS53PinctrlState *s = opaque;
    s->uart_drive[n] = level != 0;
    if (!level) { s->uart_frame_pads[n] = 0; }
    ws53_pad_uart_update(s, n);
}
/* Serializer events qualify only pads selected throughout the frame. Counts
 * and FNV1a64 hashes are virtual-peer observations, not analog measurements.
 * No global TX selector: the observed H0 signal fans out to both muxed pads. */
static void ws53_pad_uart_frame(void *opaque, int n, int event)
{
    WS53PinctrlState *s = opaque;
    if (!s->uart_tx_profile) { return; }
    if (event <= 0) {
        if (!event) {
            for (unsigned id = 0; id < 48; id++) {
                if (s->uart_frame_pads[n] & (1ULL << id)) {
                    s->uart_bytes[id]++;
                    s->uart_hash[id] = (s->uart_hash[id] ^ s->uart_byte[n]) * 1099511628211ULL;
                }
            }
        }
        s->uart_frame_pads[n] = 0;
        return;
    }
    s->uart_byte[n] = event - 1;
    s->uart_frame_pads[n] = 0;
    for (unsigned i = 0; i < ARRAY_SIZE(ws53_pad_desc); i++) {
        unsigned id = ws53_pad_desc[i].id;
        if (s->uart_drive[n] && ws53_pad_uart_tx(id, s->mode[id]) == n &&
            !(s->external_drive[id] && s->external_level[id] != s->uart_level[n])) {
            s->uart_frame_pads[n] |= 1ULL << id;
        }
    }
}

static uint64_t ws53_pinctrl_read(void *opaque, hwaddr off, unsigned size)
{
    WS53PinRegion *r = opaque;
    hwaddr addr = r->base + off;
    for (unsigned i = 0; i < ARRAY_SIZE(ws53_pad_desc); i++) {
        const WS53PadDesc *d = &ws53_pad_desc[i];
        if (d->pad == addr) { return r->owner->config[d->id]; }
        if (d->mux == addr) { return r->owner->mode[d->id]; }
    }
    qemu_log_mask(LOG_GUEST_ERROR, "ws53-pinctrl: unimplemented read 0x%" HWADDR_PRIx "\n", addr);
    return 0;
}

static void ws53_pinctrl_write(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    WS53PinRegion *r = opaque;
    WS53PinctrlState *s = r->owner;
    hwaddr addr = r->base + off;
    for (unsigned i = 0; i < ARRAY_SIZE(ws53_pad_desc); i++) {
        const WS53PadDesc *d = &ws53_pad_desc[i];
        if (d->pad == addr) {
            if ((value & ~d->pad_mask) || ((value >> 3) & 3) == 3) { break; }
            s->config[d->id] = value;
            ws53_pad_update(s, d);
            return;
        }
        if (d->mux == addr) {
            if (value > 7 || !(d->modes & (1u << value))) { break; }
            if (s->mode[d->id] != value) {
                for (unsigned n = 0; n < 3; n++) { s->uart_frame_pads[n] &= ~(1ULL << d->id); }
            }
            s->mode[d->id] = value;
            ws53_pad_update(s, d);
            if (value && !ws53_pad_pwm0(d->id, value) &&
                !(s->uart_tx_profile && (ws53_pad_uart_tx(d->id, value) >= 0 ||
                                         ws53_pad_uart_rx_port(d->id, value) >= 0)) &&
                !(s->uart_rx_profile && ws53_pad_uart_rx_port(d->id, value) >= 0)) {
                qemu_log_mask(LOG_GUEST_ERROR,
                    "ws53-pinctrl: unimplemented alternate peripheral route pad %u mode %u\n",
                    d->id, (unsigned)value);
            }
            return;
        }
    }
    qemu_log_mask(LOG_GUEST_ERROR, "ws53-pinctrl: unimplemented write 0x%" HWADDR_PRIx " value=0x%" PRIx64 "\n", addr, value);
}
static const MemoryRegionOps ws53_pinctrl_ops = {
    .read = ws53_pinctrl_read, .write = ws53_pinctrl_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 4, .max_access_size = 4 },
    .impl = { .min_access_size = 4, .max_access_size = 4 },
};
static void ws53_pinctrl_reset(void *opaque)
{
    WS53PinctrlState *s = opaque;
    memset(s->mode, 0, sizeof(s->mode));
    memset(s->traced_state, 0xff, sizeof(s->traced_state));
    for (unsigned port = 0; port < 3; port++) {
        s->uart_rx_selected[port] = -1;
        s->uart_rx_ambiguous[port] = false;
        qemu_set_irq(s->uart_rx_enable[port], 0);
    }
    for (unsigned i = 0; i < ARRAY_SIZE(ws53_pad_desc); i++) {
        const WS53PadDesc *d = &ws53_pad_desc[i];
        /* RAM boot profile, NOT asserted silicon reset values. Keep external
         * driver state, select GPIO, enable inputs, no pulls and drive code 0. */
        s->mode[d->id] = 0;
        if (s->uart_console_profile) {
            if (d->id == 17 || d->id == 18) { s->mode[d->id] = 2; }
        }
        /* Explicit observed L0 handoff without claiming H0/MGPIO17/18.
         * These are model startup profiles, never silicon reset values. */
        if ((s->uart_console_profile || s->uart_l0_console_profile) &&
            (d->id == 33 || d->id == 34)) { s->mode[d->id] = 1; }
        s->config[d->id] = 4;
        s->uart_bytes[d->id] = 0;
        s->uart_hash[d->id] = 14695981039346656037ULL;
        ws53_pad_update(s, d);
    }
    memset(s->uart_frame_pads, 0, sizeof(s->uart_frame_pads));
}
static void ws53_pinctrl_init(Object *obj)
{
    WS53PinctrlState *s = WS53_PINCTRL(obj);
    const hwaddr bases[] = { 0x52008000, 0x57036000, 0x5703a000 };
    for (unsigned i = 0; i < 3; i++) {
        s->regions[i].owner = s;
        s->regions[i].base = bases[i];
        memory_region_init_io(&s->regions[i].mmio, obj, &ws53_pinctrl_ops,
                             &s->regions[i], TYPE_WS53_PINCTRL, 0x1000);
        sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->regions[i].mmio);
    }
    qdev_init_gpio_in_named(DEVICE(obj), ws53_pad_external_level, "external-level", 48);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_pad_external_drive, "external-drive", 48);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_pad_gpio_out, "gpio-out", 48);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_pad_gpio_oe, "gpio-oe", 48);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_pad_pwm0_level, "pwm0-level", 1);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_pad_pwm0_drive, "pwm0-drive", 1);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_pad_uart_level, "uart-tx-level", 3);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_pad_uart_drive, "uart-tx-drive", 3);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_pad_uart_frame, "uart-tx-frame", 3);
    qdev_init_gpio_out_named(DEVICE(obj), s->gpio_in, "gpio-in", 48);
    qdev_init_gpio_out_named(DEVICE(obj), s->level, "level", 48);
    qdev_init_gpio_out_named(DEVICE(obj), s->drive, "drive-enable", 48);
    qdev_init_gpio_out_named(DEVICE(obj), s->floating, "floating", 48);
    qdev_init_gpio_out_named(DEVICE(obj), s->contention, "contention", 48);
    qdev_init_gpio_out_named(DEVICE(obj), s->uart_rx_level, "uart-rx-level", 3);
    qdev_init_gpio_out_named(DEVICE(obj), s->uart_rx_enable, "uart-rx-enable", 3);
    for (unsigned id = 0; id < 48; id++) {
        g_autofree char *count = g_strdup_printf("uart-tx-bytes-%u", id);
        g_autofree char *hash = g_strdup_printf("uart-tx-hash-%u", id);
        object_property_add_uint64_ptr(obj, count, &s->uart_bytes[id], OBJ_PROP_FLAG_READ);
        object_property_add_uint64_ptr(obj, hash, &s->uart_hash[id], OBJ_PROP_FLAG_READ);
    }
    qemu_register_reset(ws53_pinctrl_reset, s);
}
static const Property ws53_pinctrl_properties[] = {
    DEFINE_PROP_CHR("pad-trace", WS53PinctrlState, pad_trace),
    DEFINE_PROP_BOOL("assume-uart-tx", WS53PinctrlState, uart_tx_profile, false),
    DEFINE_PROP_BOOL("assume-uart-rx", WS53PinctrlState, uart_rx_profile, false),
    DEFINE_PROP_BOOL("uart-console-profile", WS53PinctrlState, uart_console_profile, false),
    DEFINE_PROP_BOOL("uart-l0-console-profile", WS53PinctrlState, uart_l0_console_profile, false),
};
static void ws53_pinctrl_realize(DeviceState *dev, Error **errp)
{
    WS53PinctrlState *s = WS53_PINCTRL(dev);
    if (s->uart_l0_console_profile && (s->uart_console_profile || !s->uart_tx_profile)) {
        error_setg(errp, "ws53-pinctrl: L0-only console profile requires TX model and excludes legacy console profile");
        return;
    }
    if (s->uart_console_profile && !s->uart_tx_profile) {
        error_setg(errp, "ws53-pinctrl: UART console profile requires UART TX routing");
    }
}
static void ws53_pinctrl_class_init(ObjectClass *oc, WS63_QOM_CLASS_DATA *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    dc->realize = ws53_pinctrl_realize;
    device_class_set_props(dc, ws53_pinctrl_properties);
}
static const TypeInfo ws53_pinctrl_type = {
    .name = TYPE_WS53_PINCTRL, .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(WS53PinctrlState), .instance_init = ws53_pinctrl_init,
    .class_init = ws53_pinctrl_class_init,
};

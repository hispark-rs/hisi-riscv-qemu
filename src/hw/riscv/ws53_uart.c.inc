/* WS53 SDK basic UART access profile. Included by ws53.c.
 * This owns RX FIFO/IRQ/error/timeout behavior; TX delivery is immediate and
 * does not claim baud-accurate serial framing, TX FIFO pacing or flow control.
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#define TYPE_WS53_UART "ws53-uart"
OBJECT_DECLARE_SIMPLE_TYPE(WS53UartState, WS53_UART)
#define WS53_UART_DEPTH 64

struct WS53UartState {
    SysBusDevice parent_obj;
    MemoryRegion iomem;
    CharBackend chr;
    qemu_irq irq;
    QEMUTimer *timeout;
    uint64_t rx_timeout_ns;
    uint32_t control, div_h, div_l, div_fra, baud, fifo, ier, far, modem, scratch;
    uint8_t rx[WS53_UART_DEPTH], errors[WS53_UART_DEPTH];
    unsigned head, count;
    bool overrun, timed_out;
};

static unsigned ws53_uart_capacity(WS53UartState *s)
{
    return s->fifo & 0x10 ? WS53_UART_DEPTH : 1;
}

static uint32_t ws53_uart_errors(WS53UartState *s)
{
    uint32_t result = s->overrun ? 8 : 0;
    if (s->count) {
        result |= s->errors[s->head];
    }
    if (s->fifo & 0x10) {
        for (unsigned i = 0; i < s->count; i++) {
            if (s->errors[(s->head + i) % WS53_UART_DEPTH]) {
                result |= 1;
                break;
            }
        }
    }
    return result;
}

static unsigned ws53_uart_irq_id(WS53UartState *s)
{
    static const unsigned thresholds[] = {1, 16, 32, 62};
    unsigned threshold = s->fifo & 0x10 ? thresholds[(s->fifo >> 2) & 3] : 1;
    if ((s->ier & 1) && ws53_uart_errors(s)) {
        return 6;
    }
    if ((s->ier & 4) && s->count >= threshold) {
        return 4;
    }
    if ((s->ier & 4) && s->timed_out && s->count) {
        return 12;
    }
    return 1;
}

static void ws53_uart_irq_update(WS53UartState *s)
{
    qemu_set_irq(s->irq, ws53_uart_irq_id(s) != 1);
}

static void ws53_uart_timeout(void *opaque)
{
    WS53UartState *s = opaque;
    s->timed_out = s->count != 0;
    ws53_uart_irq_update(s);
}

static void ws53_uart_rx_activity(WS53UartState *s)
{
    timer_del(s->timeout);
    s->timed_out = false;
    /* The SDK names the timeout IRQ but does not establish its duration.
     * Zero disables synthesized timeouts. Tests may inject an explicit delay;
     * it must never be presented as verified WS53 hardware timing. */
    if (s->count && s->rx_timeout_ns) {
        timer_mod(s->timeout, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + s->rx_timeout_ns);
    }
    ws53_uart_irq_update(s);
}

static void ws53_uart_push(WS53UartState *s, uint8_t value, uint8_t errors)
{
    if (s->count == ws53_uart_capacity(s)) {
        s->overrun = true;
        if (!(s->fifo & 0x10)) {
            /* SDK DATA contract: non-FIFO RBR is overwritten on overrun;
             * enabled FIFO instead retains queued data and drops the new byte. */
            s->rx[s->head] = value;
            s->errors[s->head] = errors;
        }
    } else {
        unsigned tail = (s->head + s->count++) % WS53_UART_DEPTH;
        s->rx[tail] = value;
        s->errors[tail] = errors;
    }
    ws53_uart_rx_activity(s);
}

static uint64_t ws53_uart_read(void *opaque, hwaddr off, unsigned size)
{
    WS53UartState *s = opaque;
    switch (off) {
    case 0x00: return ws53_uart_irq_id(s) | (s->fifo & 0x10);
    case 0x04: {
        uint8_t result = 0;
        if (s->count) {
            result = s->rx[s->head];
            s->head = (s->head + 1) % WS53_UART_DEPTH;
            s->count--;
        }
        ws53_uart_rx_activity(s);
        qemu_chr_fe_accept_input(&s->chr);
        return result;
    }
    case 0x08: return s->control;
    case 0x0c: return s->div_h;
    case 0x10: return s->div_l;
    case 0x14: return s->div_fra;
    case 0x18: return s->ier;
    case 0x28: return s->far;
    case 0x2c: return s->modem;
    case 0x34: {
        uint32_t result = 0xc0 | (s->count ? 0x20 : 0) | ws53_uart_errors(s);
        if (s->count) {
            s->errors[s->head] = 0;
        }
        s->overrun = false;
        ws53_uart_irq_update(s);
        return result;
    }
    case 0x38: return s->scratch;
    case 0x44: return 2 | (s->count ? 4 : 0) |
                       (s->count == ws53_uart_capacity(s) ? 8 : 0);
    case 0x48: return 0; /* TX is delivered immediately in this model. */
    case 0x4c: return s->count;
    case 0x58: return s->baud;
    case 0x60: return 0x204; /* Model-selected 64-byte FIFO / 32-bit access. */
    }
    qemu_log_mask(LOG_GUEST_ERROR, "ws53-uart: unimplemented read +0x%" HWADDR_PRIx "\n", off);
    return 0;
}

static void ws53_uart_write(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    WS53UartState *s = opaque;
    switch (off) {
    case 0x04: {
        uint8_t byte = value;
        if (s->modem & 2) {
            ws53_uart_push(s, byte, 0);
        } else {
            qemu_chr_fe_write_all(&s->chr, &byte, 1);
        }
        return;
    }
    case 0x08: s->control = value & 0xff; ws53_uart_rx_activity(s); return;
    case 0x0c: s->div_h = value & 0xff; ws53_uart_rx_activity(s); return;
    case 0x10: s->div_l = value & 0xff; ws53_uart_rx_activity(s); return;
    case 0x14: s->div_fra = value & 0x3f; ws53_uart_rx_activity(s); return;
    case 0x18:
        if (value & ~5u) {
            break; /* TX/modem IRQ and programmable THRE remain unsupported. */
        }
        s->ier = value;
        ws53_uart_irq_update(s);
        return;
    case 0x24:
        if (((s->fifo ^ value) & 0x10) && !(value & 0x40)) {
            break; /* Mode changes without an explicit RX reset are unmodeled. */
        }
        if (value & 0x40) {
            s->head = s->count = 0;
            s->overrun = false;
        }
        s->fifo = value & 0x1f; /* Reset commands do not latch. */
        ws53_uart_rx_activity(s);
        qemu_chr_fe_accept_input(&s->chr);
        return;
    case 0x28: s->far = value & 1; return;
    case 0x2c:
        if (value & ~2u) {
            break; /* No CTS/RTS/DTR/auto-flow model yet. */
        }
        s->modem = value;
        return;
    case 0x38: s->scratch = value & 0xff; return;
    case 0x40:
        if (s->far) {
            ws53_uart_push(s, value, ((value & 0x100) ? 4 : 0) |
                                    ((value & 0x200) ? 2 : 0));
        }
        return;
    case 0x58:
        if ((value & 15) != 7 && (value & 15) != 15) {
            break;
        }
        s->baud = value & 0xff;
        ws53_uart_rx_activity(s);
        return;
    }
    qemu_log_mask(LOG_GUEST_ERROR, "ws53-uart: unimplemented write +0x%" HWADDR_PRIx " value=0x%" PRIx64 "\n", off, value);
}

static const MemoryRegionOps ws53_uart_ops = {
    .read = ws53_uart_read, .write = ws53_uart_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 4, .max_access_size = 4 },
    .impl = { .min_access_size = 4, .max_access_size = 4 },
};

static int ws53_uart_can_receive(void *opaque)
{
    WS53UartState *s = opaque;
    return ws53_uart_capacity(s) - s->count;
}

static void ws53_uart_receive(void *opaque, const uint8_t *buf, int size)
{
    WS53UartState *s = opaque;
    for (int i = 0; i < size; i++) {
        ws53_uart_push(s, buf[i], 0);
    }
}

static void ws53_uart_reset(void *opaque)
{
    WS53UartState *s = opaque;
    timer_del(s->timeout);
    s->head = s->count = 0;
    s->overrun = s->timed_out = false;
    s->ier = s->far = s->modem = s->scratch = s->div_h = 0;
    /* Explicit configured-console model handoff, not silicon reset facts. */
    s->control = 0x0c;
    s->div_l = 17;
    s->div_fra = 23;
    s->baud = 0x7f;
    s->fifo = 0x10;
    ws53_uart_irq_update(s);
}

static void ws53_uart_realize(DeviceState *dev, Error **errp)
{
    WS53UartState *s = WS53_UART(dev);
    s->timeout = timer_new_ns(QEMU_CLOCK_VIRTUAL, ws53_uart_timeout, s);
    qemu_chr_fe_set_handlers(&s->chr, ws53_uart_can_receive, ws53_uart_receive,
                            NULL, NULL, s, NULL, true);
    qemu_register_reset(ws53_uart_reset, s);
}

static const Property ws53_uart_properties[] = {
    DEFINE_PROP_CHR("chardev", WS53UartState, chr),
    DEFINE_PROP_UINT64("rx-timeout-ns", WS53UartState, rx_timeout_ns, 0),
};

static void ws53_uart_init(Object *obj)
{
    WS53UartState *s = WS53_UART(obj);
    memory_region_init_io(&s->iomem, obj, &ws53_uart_ops, s, TYPE_WS53_UART, 0x1000);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->iomem);
    sysbus_init_irq(SYS_BUS_DEVICE(obj), &s->irq);
}

static void ws53_uart_class_init(ObjectClass *oc, WS63_QOM_CLASS_DATA *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    dc->realize = ws53_uart_realize;
    device_class_set_props(dc, ws53_uart_properties);
}

static const TypeInfo ws53_uart_type = {
    .name = TYPE_WS53_UART, .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(WS53UartState), .instance_init = ws53_uart_init,
    .class_init = ws53_uart_class_init,
};

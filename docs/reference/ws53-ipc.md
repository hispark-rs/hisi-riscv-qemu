# WS53 application IPC doorbells

The opt-in `-global ws53-ipc.assume-profile=on` digital profile models two
sticky doorbells, application receive IRQ26/27, enable masks, full-word ACK,
discard-only word readback and patched halfword send. Default access and invalid
widths/commands return bus errors with diagnostics; missing behavior does not
silently pass through a zero-valued register store. No reset values are asserted
for silicon. The exact SDK/RDL evidence lives in hisi-registers b449799.

The device occupies0x52000020..0x5200003f, disjoint from the peripheral clock
slots. `peer` input0/1 pulses inject incoming notifications; input2/3 consumes
outgoing notifications. `clock` input0 gates access and IRQ output, retaining
existing pending state; `fault` input0 holds incoming ACK. These are virtual
peer/fault assumptions, not control-core firmware or measured clock semantics.
ACK readback returns an intentionally unrelated marker; software must discard it,
recheck queue/pending and never treat readback as buffer release or cache drain.
Only release command1 is accepted, without inventing reset/flush semantics.

`ws53_ipc_test.py` exercises masked arrival, independent channel ACK, reenable,
transmit/peer acknowledgement, held ACK, clock failure, invalid access and three
reset rounds. The aggregate suite includes it. IRQ numbers and register layout
alone do not prove ring-message exchange or peripheral ownership negotiation.

No shared ring, remote-pointer parser, TRNG service, full control-core firmware,
physical power/reset timing, cache coherence, DMA drain or low-power handshake
is implemented here. The generic SDK shared-memory defaults are not used: the
actual application takes the origin from its retained control-image header.

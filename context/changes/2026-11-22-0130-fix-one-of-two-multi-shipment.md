One mail with several shipments (board reason one_of_two, rounds 89/90): extraction fills `additional_shipments`, each shipment is priced in parallel with its own bid slot (`bids.slot_for`), and ONE deterministic quote mail carries a separate EUR all-in total per shipment. No flag, no extra LLM call.
Not verified: live round; booking still books only the first shipment's quote.

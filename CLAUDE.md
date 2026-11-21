# 5U AI Hackathon — START HACK Munich 2026

We are building a digital air forwarder for the 5U AI challenge: an autonomous system that reads one mailbox, routes every mail, prices air export RFQs (rate sheets, rate portal, carrier bids), quotes, books by rate ID and answers tracking questions.

**Full challenge context:** [docs/CHALLENGE.md](docs/CHALLENGE.md) — read it before designing or changing anything.
Original PDFs: [docs/5U-AI-challenge-brief.pdf](docs/5U-AI-challenge-brief.pdf), [docs/5U-AI-challenge-deep-dive.pdf](docs/5U-AI-challenge-deep-dive.pdf).

## Key constraints

- New RFQ every 6 min, expires after 6 min. Full time points only within 2 min.
- Quote = one option (cheapest or fastest, as requested), one all-in total with currency, reply in the customer's thread; only the first quote counts.
- Accepted only within 5 % of the best price. Carrier bids: pick the most balanced (price/speed 50/50).
- LLM calls only via the 5U gateway (OpenAI models). Cost score is zero at 0.10 USD per quote; a quote with no gateway call earns no cost points.
- Rate portal is rate limited; rate IDs valid 15 min.
- Setup changes ~hourly without notice — build tolerant parsers and good logging.
- Required components: router + up to 3 agents (RFQ/booking, tracking, tariff ingestion), pricing module, CRM with dashboard, sessions UI with streaming and human intervention.
- Judged also on pitch, UX/design, codebase quality and how human the emails read.

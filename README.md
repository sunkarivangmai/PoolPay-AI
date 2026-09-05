# PoolPay AI

> **Autonomous Multi-Agent Group Buying & Dynamic Razorpay Payment Rails**

PoolPay AI enables autonomous Buyer AI Agents to pool purchase demand, negotiate wholesale volume discounts with a Merchant Negotiator AI Agent, sign AP2-inspired Intent Mandates, and execute Razorpay batch orders with built-in graceful failure recovery.

---

## Tech Stack

| Layer | Technology | Description |
|---|---|---|
| **Agent Framework** | Python State Machines | Buyer Agent & Merchant Negotiator Agent |
| **Agent Protocol** | AP2 Intent Mandates | Cryptographically signed HMAC SHA-256 intent delegation |
| **Payments & Rails** | Razorpay Test API & Webhooks | Orders API, Payment Links, Webhook Listener |
| **Backend & API** | FastAPI (Python) | REST API, SQLite persistence, and Webhook listener |
| **Frontend Dashboard** | Next.js 14 + TypeScript + Tailwind CSS | Dark-mode dashboard and live negotiation interface |

---

## Repository Structure

```text
PoolPay AI/
├── agents/
│   ├── buyer_agent.py
│   ├── merchant_agent.py
│   ├── negotiation_agent.py
│   ├── orchestrator.py
│   ├── payment_agent.py
│   └── recovery_agent.py
├── core/
│   ├── config.py
│   ├── database.py
│   ├── razorpay_client.py
│   └── schemas.py
├── server/
│   ├── main.py
│   └── webhooks.py
├── web/
│   └── Next.js frontend dashboard
├── requirements.txt
└── README.md
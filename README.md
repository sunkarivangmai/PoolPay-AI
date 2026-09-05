<<<<<<< HEAD
# PoolPay AI

> **Autonomous Multi-Agent Group Buying & Dynamic Razorpay Payment Rails**

PoolPay AI enables autonomous Buyer AI Agents to pool purchase demand, negotiate wholesale volume discounts with a Merchant Negotiator AI Agent, sign AP2-inspired Intent Mandates, and execute Razorpay batch orders seamlessly with built-in graceful failure recovery.

---

## 🛠️ Tech Stack

| Layer | Technology | Description |
|---|---|---|
| **Agent Framework** | Python State Machines | Buyer Agent & Merchant Negotiator Agent |
| **Agent Protocol** | AP2 Intent Mandates | Cryptographically signed HMAC SHA-256 intent delegation |
| **Payments & Rails** | Razorpay Test API & Webhooks | Orders API, Payment Links, Webhook Listener |
| **Backend & API** | FastAPI (Python) | REST API, SQLite persistence, and Webhook listener |
| **Frontend Dashboard** | Next.js 14 (TypeScript) + Tailwind CSS | Dark-mode glassmorphic interface & live negotiation terminal |

---

## 📁 Repository Structure

```
PoolPay AI/
├── agents/
│   ├── buyer_agent.py        # Generates & cryptographically signs AP2 Intent Mandates
│   └── merchant_agent.py     # Evaluates wholesale margins & triggers Razorpay orders
├── core/
│   ├── config.py             # App & Razorpay configuration
│   ├── database.py           # SQLAlchemy SQLite setup & seed products
│   ├── razorpay_client.py    # Wrapper for Razorpay Orders & Payment Links API
│   └── schemas.py            # Mandate, AuditLog, Product & Pool Pydantic models
├── server/
│   ├── main.py               # FastAPI server application
│   └── webhooks.py           # Razorpay webhook listener (payment.authorized, payment.failed)
├── web/                      # Next.js 14 visual dashboard & pitch demo UI
├── requirements.txt          # Python dependencies
└── README.md                 # System setup & architecture guide
```

---

## ⚡ Quick Start

### 1. Install & Run Backend Server

```bash
# Install Python dependencies
pip install -r requirements.txt

# Start FastAPI Server (runs at http://127.0.0.1:8000)
python -m uvicorn server.main:app --host 127.0.0.1 --port 8000
```

### 2. Start Next.js Frontend Dashboard

```bash
cd web
npm install
npm run dev
```

Open **[http://localhost:3000](http://localhost:3000)** in your browser to experience the live dashboard!

---

## 🛡️ Hackathon Resiliency & Failure Recovery Demo

PoolPay AI includes a built-in interactive scenario simulating a **1 out of 5 payment authorization failure** (e.g. card declined):
1. In the Next.js dashboard, select a product (e.g., *Apple AirPods Pro*).
2. Click **Auto-Fill 4 AI Bids** and then submit a 5th Buyer Mandate to reach pool threshold.
3. Observe the Merchant Agent automatically trigger a **Razorpay Batch Order**.
4. Click **Simulate 1/5 Payment Failure & Auto-Recover** to observe the Merchant Agent execute the **Graceful Recovery Protocol** (absorbing the shortfall from profit margin buffer without crashing the batch deal).
=======
# PoolPay-AI
>>>>>>> 85a2182ac015766bc2088a2f458c50d61ec3bf7f

from datetime import datetime, timezone
from sqlalchemy.orm import Session
from core.database import DBPoolDeal, DBProduct, DBNegotiationEvent, emit_audit_event
from core.schemas import NegotiationDecision, EventType, AuditActor, DealStatus
from agents.merchant_agent import calculate_discount_percent, calculate_discounted_price


class NegotiationAgent:
    """
    Negotiation Agent: Runs structured buyer-merchant negotiation rounds.

    Loop:
      Buyer Offer → Merchant Counter → Validation → Accept/Reject

    AI vs DETERMINISTIC:
    - Price calculations: DETERMINISTIC
    - Acceptance threshold: DETERMINISTIC (within 5% = accept)
    - Reasoning text: TEMPLATE (could be LLM-generated)
    """

    def __init__(self):
        self.name = "NegotiationAgent"

    def run_negotiation(self, db: Session, deal: DBPoolDeal, product: DBProduct,
                        buyer_target_price: float, pool_size: int) -> list:
        """
        Execute multi-round negotiation and persist each round.
        Returns list of negotiation events.
        """
        rounds = []
        max_rounds = 3
        old_status = deal.status
        deal.status = DealStatus.NEGOTIATING.value

        emit_audit_event(
            db, EventType.NEGOTIATION_STARTED.value,
            actor=self.name, actor_type=AuditActor.NEGOTIATION_AGENT.value,
            summary=f"Negotiation started for pool {deal.deal_id[:8]}",
            reasoning=f"Buyer target: {buyer_target_price:.0f}, Pool size: {pool_size}",
            pool_id=deal.deal_id,
            previous_state=old_status, new_state=DealStatus.NEGOTIATING.value,
            metadata={"buyer_target": buyer_target_price, "pool_size": pool_size}
        )

        current_buyer_offer = buyer_target_price
        merchant_counter = product.retail_price  # Start at retail

        for round_num in range(1, max_rounds + 1):
            # Merchant calculates offer based on pool size
            discount = calculate_discount_percent(pool_size)
            merchant_counter = calculate_discounted_price(product.retail_price, discount)

            # Ensure merchant counter doesn't go below wholesale floor
            if merchant_counter < product.min_wholesale_price:
                merchant_counter = product.min_wholesale_price + 100

            # Buyer adjusts offer upward each round (concession strategy)
            if round_num > 1:
                gap = merchant_counter - current_buyer_offer
                current_buyer_offer = round(current_buyer_offer + gap * 0.4, 2)

            # Decision: accept if within 5% of each other
            price_gap_percent = abs(merchant_counter - current_buyer_offer) / merchant_counter * 100

            if price_gap_percent <= 5.0:
                decision = NegotiationDecision.ACCEPT
                reasoning = (
                    f"Round {round_num}: Price gap ({price_gap_percent:.1f}%) within 5% threshold. "
                    f"Deal accepted at merchant price {merchant_counter:.0f}."
                )
            elif round_num == max_rounds:
                decision = NegotiationDecision.ACCEPT
                reasoning = (
                    f"Round {round_num}: Final round reached. "
                    f"Accepting merchant offer of {merchant_counter:.0f} (gap: {price_gap_percent:.1f}%)."
                )
            else:
                decision = NegotiationDecision.COUNTER_OFFER
                reasoning = (
                    f"Round {round_num}: Gap of {price_gap_percent:.1f}%. "
                    f"Buyer offers {current_buyer_offer:.0f}, merchant counters {merchant_counter:.0f}. "
                    f"Continuing negotiation."
                )

            event = DBNegotiationEvent(
                deal_id=deal.deal_id,
                round_number=round_num,
                buyer_agent="BuyerAgent",
                merchant_agent="MerchantAgent",
                pool_size=pool_size,
                buyer_offer=current_buyer_offer,
                merchant_counter=merchant_counter,
                discount_percent=discount,
                decision=decision.value,
                reasoning=reasoning
            )
            db.add(event)

            event_type = EventType.OFFER_ACCEPTED if decision == NegotiationDecision.ACCEPT else EventType.COUNTER_OFFER_CREATED
            emit_audit_event(
                db, event_type.value,
                actor=self.name, actor_type=AuditActor.NEGOTIATION_AGENT.value,
                summary=f"Round {round_num}: {decision.value} — Buyer {current_buyer_offer:.0f} vs Merchant {merchant_counter:.0f}",
                reasoning=reasoning,
                pool_id=deal.deal_id,
                metadata={"round": round_num, "buyer_offer": current_buyer_offer,
                           "merchant_counter": merchant_counter, "discount_percent": discount,
                           "gap_percent": round(price_gap_percent, 1)}
            )

            rounds.append({
                "round_number": round_num,
                "buyer_offer": current_buyer_offer,
                "merchant_counter": merchant_counter,
                "discount_percent": discount,
                "decision": decision.value,
                "reasoning": reasoning
            })

            if decision == NegotiationDecision.ACCEPT:
                deal.negotiated_price = merchant_counter
                deal.discount_percent = discount
                deal.status = DealStatus.OPEN.value
                break

        db.commit()
        return rounds


negotiation_agent = NegotiationAgent()

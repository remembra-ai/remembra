"""
Set up Stripe products and prices for Remembra Cloud.
Run once to create the Pro plan in Stripe.
"""
import stripe
import os

# Load Stripe key from environment
import os
stripe.api_key = os.environ.get("REMEMBRA_STRIPE_SECRET_KEY") or os.environ.get("STRIPE_SECRET_KEY")
if not stripe.api_key:
    raise ValueError("Set REMEMBRA_STRIPE_SECRET_KEY or STRIPE_SECRET_KEY environment variable")

def create_pro_plan():
    """Create the Remembra Pro product and price."""
    
    # Create the product
    product = stripe.Product.create(
        name="Remembra Pro",
        description="AI Memory Infrastructure - 100K memories, 500K recalls/mo, 10 API keys",
        metadata={
            "plan_tier": "pro",
        }
    )
    print(f"✅ Created Product: {product.id}")
    
    # Create the price ($49/month)
    price = stripe.Price.create(
        product=product.id,
        unit_amount=4900,  # $49.00 in cents
        currency="usd",
        recurring={
            "interval": "month",
        },
        metadata={
            "plan_tier": "pro",
        }
    )
    print(f"✅ Created Price: {price.id}")
    print(f"\n📋 Update plans.py with:")
    print(f'   stripe_price_id="{price.id}"')
    
    return product.id, price.id

if __name__ == "__main__":
    product_id, price_id = create_pro_plan()
    print(f"\n🎉 Done! Product: {product_id}, Price: {price_id}")

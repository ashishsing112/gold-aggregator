# GoldLens — jeweller-published gold rates

GoldLens compares the 24K per-gram rate published in each jeweller's own gold-rate section. It does **not** scrape coin or jewellery product prices.

## Included sources

- Tanishq — official India gold-rate page
- Dhan — India 24K rate page
- Malabar Gold & Diamonds — Pan India live metal-rate feed
- CaratLane — India gold-rate page (the page notes that city rates can vary)
- Candere by Kalyan — Mumbai gold-rate page
- Indriya — published 24K/999 store-rate feed
- PNG Jewellers — published online-purchase metal rate

Sites without a usable, automatically readable gold-rate section are not listed. Product marketplaces, digital-gold sellers, delivery apps, coin listings, and placeholder sellers have been removed.

These are reference gold rates only. They exclude making charges, premiums, GST, stones, delivery, and other fees.

## Run

Requires Python 3.10 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload
```

Open <http://127.0.0.1:8000>. Force an update with <http://127.0.0.1:8000/api/rates?refresh=true>.

## Test

```bash
pip install -r requirements-dev.txt
pytest -q
```

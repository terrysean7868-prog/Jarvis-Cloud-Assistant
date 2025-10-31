DESCRIPTION = "Currency Converter: Convert between different currencies using real-time exchange rates."

def register(dp, services, scheduler):
    """
    Register the /convert command:
    Usage: /convert <amount> <from_currency> <to_currency>
    Example: /convert 100 USD INR
    """
    from telegram.ext import CommandHandler
    import requests
    import logging

    logger = logging.getLogger("jarvis.currency_converter")

    API_URL = "https://api.exchangerate-api.com/v4/latest/"

    def fetch_exchange_rates(base_currency):
        """Fetch exchange rates for base_currency. Returns (data, error)."""
        try:
            r = requests.get(f"{API_URL}{base_currency}", timeout=10)
            r.raise_for_status()
            return r.json(), None
        except requests.RequestException as e:
            logger.exception("Exchange rate fetch failed")
            return None, str(e)

    def convert_cmd(update, context):
        args = context.args or []
        if len(args) != 3:
            update.message.reply_text("Usage: /convert <amount> <from_currency> <to_currency>\nExample: /convert 100 USD INR")
            return

        # Parse inputs
        raw_amount, from_cur, to_cur = args[0], args[1].upper(), args[2].upper()
        try:
            amount = float(raw_amount)
        except ValueError:
            update.message.reply_text("Please provide a valid numeric amount, e.g. 100 or 12.5")
            return

        # Fetch rates
        data, err = fetch_exchange_rates(from_cur)
        if err:
            update.message.reply_text(f"Failed to fetch rates for {from_cur}: {err}")
            return

        rates = data.get("rates") or {}
        if to_cur not in rates:
            update.message.reply_text(f"Conversion rate for {to_cur} not available (base {from_cur}).")
            return

        rate = rates[to_cur]
        converted = amount * rate
        update.message.reply_text(f"{amount:g} {from_cur} ≈ {converted:,.2f} {to_cur}\nRate: 1 {from_cur} = {rate:.6f} {to_cur}")

    dp.add_handler(CommandHandler("convert", convert_cmd))

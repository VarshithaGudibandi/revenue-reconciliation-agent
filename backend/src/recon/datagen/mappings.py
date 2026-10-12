from recon.datagen.styles import INVOICE_STYLES, PAYMENT_STYLES

# How the generator's default exports map to canonical fields.
INVOICE_MAPPING = INVOICE_STYLES["default"].mapping()
PAYMENT_MAPPING = PAYMENT_STYLES["default"].mapping()

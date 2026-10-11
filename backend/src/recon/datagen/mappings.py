from recon.ingestion.normalize import ColumnMapping

# How the generator's two exports map to canonical fields.
INVOICE_MAPPING = ColumnMapping(
    record_id="invoice_id",
    counterparty="customer_id",
    date="issue_date",
    amount="total",
    currency="currency",
    date_format="%Y-%m-%d",
)

PAYMENT_MAPPING = ColumnMapping(
    record_id="payment_id",
    counterparty="customer",
    date="received_on",
    amount="amount_paid",
    currency="ccy",
    reference="reference",
    date_format="%m/%d/%Y",
)

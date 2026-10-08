-- Sold used Retail/Lease deals: the history every comparable pool is built from.
-- Output columns match the app's `deals` table (app/db.py), so cohorts.load_frame runs unchanged.
-- Reproduces the Advent parser's rules (app/ingest/sales.py):
-- upper-cased codes, 17-char VINs, one row per store + deal + VIN, total gross falls back to front + back,
-- days_to_sell null outside 0-2000.
with s as (
	select
		upper(ltrim(rtrim(dealercode))) as dealer_code,
		cast(dealnumber as varchar(30)) as deal_number,
		upper(ltrim(rtrim(vin))) as vin,
		case when [Sale Type] = 'Retail' then 'Retail' else 'Lease' end as sale_type,  -- exact casing; the app compares strings
		cast([RS Date] as date) as sold_date,                                           -- CONFIRM: RS Date = sold date
		[year],
		upper(make) as make,
		upper(model) as model,
		mileage,
		[Veh Front Gross] as front_gross,
		Back_Gross as back_gross,
		case when [Veh Front Gross] is null and Back_Gross is null then null
		     else isnull([Veh Front Gross], 0) + isnull(Back_Gross, 0) end as total_gross,  -- plain a + b is null if either side is null
		case when [Age In Invt] between 0 and 2000 then [Age In Invt] end as days_to_sell, -- CONFIRM: = sold date - receive date
		[Sales Price] as sold_price,
		row_number() over (partition by upper(ltrim(rtrim(dealercode))), cast(dealnumber as varchar(30)), upper(ltrim(rtrim(vin)))
		                   order by [RS Date] desc) as rn  -- same keys as the output: the batch's deals table rejects duplicates
	from FactSales
	where [RS Date] >= dateadd(month, -6, getdate())
	  and [Sale Type] in ('Retail', 'Lease')
	  and dealstatus in ('Finalized', 'RSed')
	  and [New/Used] = 'Used'
	  and dealercode not in ('FRMBN', 'FRPOR', 'AUDFR', 'WCPOR')
	  and dealnumber is not null
)
select dealer_code, deal_number, vin, sale_type, sold_date, [year], make, model, mileage,
       front_gross, back_gross, total_gross, days_to_sell, sold_price
from s
where rn = 1
  and len(vin) = 17;

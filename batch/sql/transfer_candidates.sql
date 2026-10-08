-- Every used vehicle in stock. The script places all of them; Tableau filters on days_in_stock.
-- dealer_code is the vehicle's current store and must use the same codes as FactSales.dealercode
-- (NBMBN, BHMBN, ... see app/config.py STORES). Otherwise is_current_store never matches.
-- One row per VIN (a car with two stock records would otherwise be placed twice).
with c as (
select
	[Stock#] as stock_no,
	[year],
	upper(make) as make,
	upper(model) as model,
	try_convert(date, [Received Date], 1) as receive_date,  -- style 1 = mm/dd/yy
	datediff(day, try_convert(date, [Received Date], 1), cast(getdate() as date)) as days_in_stock,
	Miles as mileage,                                       -- required: without it the 10,000-mile limit is skipped
	upper(ltrim(rtrim(StoreID))) as dealer_code,            -- CONFIRM: same codes as FactSales.dealercode
	Dealercost as dealer_cost,
	upper(ltrim(rtrim(vin))) as vin,
	row_number() over (partition by upper(ltrim(rtrim(vin))) order by try_convert(date, [Received Date], 1) desc) as rn
from Current_Inventory
where [Veh Status] = 'P'
  and [User Status] = 'W'
  and [New/Used] = 'U'
  and len(ltrim(rtrim(vin))) = 17
  and StoreID not in ('FRMBN', 'FRPOR', 'AUDFR', 'WCPOR')
)
select stock_no, [year], make, model, receive_date, days_in_stock, mileage, dealer_code, dealer_cost, vin
from c
where rn = 1;

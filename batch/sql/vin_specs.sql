-- One-time: the decode table. Same columns as the app's vin_specs (app/db.py) plus decode_attempts.
-- Seed it with:  python -m batch.run seed-decodes .claude\decode-cache-2026-10-02.jsonl.gz
create table dbo.vin_specs (
	vin char(17) not null primary key,
	source varchar(40), decoded_at varchar(20), error nvarchar(400),
	[year] int, make nvarchar(60), model nvarchar(120), [trim] nvarchar(120), [version] nvarchar(200),
	body_type nvarchar(60), vehicle_type nvarchar(60), drivetrain nvarchar(40), transmission nvarchar(60),
	engine nvarchar(120), cylinders int, fuel_type nvarchar(60), powertrain_type nvarchar(60), msrp float,
	ext_color nvarchar(120), ext_base nvarchar(60), int_color nvarchar(120), int_base nvarchar(60),
	packages nvarchar(max), mfr_code nvarchar(40), options nvarchar(max), raw nvarchar(max),
	decode_attempts int not null default 0
);

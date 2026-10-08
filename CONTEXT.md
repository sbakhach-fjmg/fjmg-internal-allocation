# Used vehicle placement

Deciding whether a used vehicle aging on one store's lot should stay or be transferred to another Fletcher Jones store, based on how comparable used cars have sold at each store.

## Language

**Store**:
One active Fletcher Jones rooftop, identified by its dealer code. Former stores (WCPOR, FRMBN, AUDFR, FRPOR) are never stores here.
_Avoid_: Dealer, location, rooftop

**Current store**:
The store whose lot a used vehicle is on today.
_Avoid_: Home store, origin store

**Days in stock**:
How long a used vehicle has been on its current store's lot. It is what makes a vehicle worth reviewing for a transfer.
_Avoid_: Age (ambiguous with model year), aging days

**Transfer candidate**:
A used vehicle in stock that is being reviewed for a possible transfer, usually because its days in stock are high.
_Avoid_: Incoming vehicle, aged unit, unit to place

**Transfer**:
Moving a vehicle from its current store to another store.
_Avoid_: Reallocation, move, swap

**Deal**:
One used Retail or Lease sale at a store, with its front, back and total gross and its receive-to-sold days. Wholesale is never a deal.
_Avoid_: Sale, transaction

**Decode**:
The stored MarketCheck description of a VIN (trim, version, manufacturer code, engine, colors, options). Made once per VIN and kept forever.
_Avoid_: Spec, VIN lookup

**Comparable pool**:
The deals judged similar enough to a transfer candidate to predict how it will do at each store.
_Avoid_: Cohort (reserved for the analysis pages' model × trim × year groupings), matches

**Match level**:
How specific the comparable pool is: Exact, Same version, Same trim or Same model, plus the year and mileage widening used.
_Avoid_: Fallback, confidence

**Thin pool**:
A comparable pool where no level reached the minimum deal count, so the most specific non-empty pool was used anyway.
_Avoid_: Low data, sparse

**Ranking rule**:
The ordered list of criteria, tie bands and thresholds that turns per-store scores into a store ranking. There is one ranking rule, owned by the analytics team.
_Avoid_: Logic, settings, criteria order

**Placement**:
The ranked list of stores, including its current store, for one transfer candidate from one run, with the match level and a reason for each store's rank.
_Avoid_: Suggestion, recommendation, allocation

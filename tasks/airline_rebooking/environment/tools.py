"""The 14 airline tools, operating on an in-memory AirlineDB (dict-backed).

Write-tool mutation semantics mirror tau2-bench's airline tools
(src/tau2/domains/airline/tools.py @ the commit in provenance/airline/TAU2_COMMIT.txt) so that
applying the golden actions reproduces a faithful DB state. Reads return the relevant records. The
DB-state oracle only depends on the WRITE tools; reads exist so a real agent can act.

Reference time is fixed at 2024-05-15T15:00:00 (the environment clock, matching the policy).
"""
from __future__ import annotations

from copy import deepcopy

from .db import AirlineDB

NOW = "2024-05-15T15:00:00"

READ_ONLY = {
    "get_user_details", "get_reservation_details", "search_direct_flight", "search_onestop_flight",
    "list_all_airports", "get_flight_status", "calculate",
}
# transfer_to_human_agents is non-mutating too, but is its own category (not a DB read).
NON_WRITE = READ_ONLY | {"transfer_to_human_agents"}


class ToolError(ValueError):
    """A tool precondition failed (e.g. unavailable flight, bad payment). Mirrors tau2 ValueError."""


class AirlineTools:
    """Callable tool surface over an AirlineDB. `execute(name, arguments)` dispatches by tool name."""

    def __init__(self, db: AirlineDB):
        self.db = db

    # --- internal helpers (mirror tau2 _get_* / _payment_for_update) ----------------------------
    def _user(self, user_id: str) -> dict:
        if user_id not in self.db.users:
            raise ToolError(f"User {user_id} not found")
        return self.db.users[user_id]

    def _reservation(self, reservation_id: str) -> dict:
        if reservation_id not in self.db.reservations:
            raise ToolError(f"Reservation {reservation_id} not found")
        return self.db.reservations[reservation_id]

    def _flight(self, flight_number: str) -> dict:
        if flight_number not in self.db.flights:
            raise ToolError(f"Flight {flight_number} not found")
        return self.db.flights[flight_number]

    def _flight_instance(self, flight_number: str, date: str) -> dict:
        flight = self._flight(flight_number)
        dates = flight.get("dates", {})
        if date not in dates:
            raise ToolError(f"Flight {flight_number} not found on date {date}")
        return dates[date]

    def _new_reservation_id(self) -> str:
        for rid in ["HATHAT", "HATHAU", "HATHAV"]:
            if rid not in self.db.reservations:
                return rid
        raise ToolError("Too many reservations")

    def _new_payment_ids(self) -> list[int]:
        return [3221322, 3221323, 3221324]

    def _payment_for_update(self, user: dict, payment_id: str, total_price: int):
        """Validate + deduct for an update; return a payment record (or None if price 0). Mirrors tau2."""
        methods = user.get("payment_methods", {})
        if payment_id not in methods:
            raise ToolError("Payment method not found")
        pm = methods[payment_id]
        if pm.get("source") == "certificate":
            raise ToolError("Certificate cannot be used to update reservation")
        if pm.get("source") == "gift_card" and pm.get("amount", 0) < total_price:
            raise ToolError("Gift card balance is not enough")
        if pm.get("source") == "gift_card":
            pm["amount"] -= total_price
        if total_price != 0:
            return {"payment_id": payment_id, "amount": total_price}
        return None

    # --- WRITE tools (DB-mutating; semantics mirror tau2) ----------------------------------------
    def book_reservation(self, user_id, origin, destination, flight_type, cabin, flights,
                         passengers, payment_methods, total_baggages, nonfree_baggages, insurance):
        user = self._user(user_id)
        rid = self._new_reservation_id()
        reservation = {
            "reservation_id": rid, "user_id": user_id, "origin": origin, "destination": destination,
            "flight_type": flight_type, "cabin": cabin, "flights": [],
            "passengers": deepcopy(passengers), "payment_history": deepcopy(payment_methods),
            "created_at": NOW, "total_baggages": total_baggages,
            "nonfree_baggages": nonfree_baggages, "insurance": insurance,
        }
        total_price = 0
        touched = []
        for fi in flights:
            fnum, date = fi["flight_number"], fi["date"]
            flight = self._flight(fnum)
            inst = self._flight_instance(fnum, date)
            if inst.get("status") != "available":
                raise ToolError(f"Flight {fnum} not available on date {date}")
            if inst.get("available_seats", {}).get(cabin, 0) < len(passengers):
                raise ToolError(f"Not enough seats on flight {fnum}")
            price = inst["prices"][cabin]
            reservation["flights"].append({
                "origin": flight["origin"], "destination": flight["destination"],
                "flight_number": fnum, "date": date, "price": price,
            })
            touched.append(inst)
            total_price += price * len(passengers)
        if insurance == "yes":
            total_price += 30 * len(passengers)
        total_price += 50 * nonfree_baggages
        for pm in payment_methods:
            pid = pm["payment_id"]
            if pid not in user.get("payment_methods", {}):
                raise ToolError(f"Payment method {pid} not found")
            upm = user["payment_methods"][pid]
            if upm.get("source") in {"gift_card", "certificate"} and upm.get("amount", 0) < pm["amount"]:
                raise ToolError(f"Not enough balance in payment method {pid}")
        if sum(pm["amount"] for pm in payment_methods) != total_price:
            raise ToolError("Payment amount does not add up")
        for pm in payment_methods:
            pid = pm["payment_id"]; upm = user["payment_methods"][pid]
            if upm.get("source") == "gift_card":
                upm["amount"] -= pm["amount"]
            elif upm.get("source") == "certificate":
                user["payment_methods"].pop(pid)
        for inst in touched:
            inst["available_seats"][cabin] -= len(passengers)
        self.db.reservations[rid] = reservation
        user.setdefault("reservations", []).append(rid)
        return reservation

    def update_reservation_flights(self, reservation_id, cabin, flights, payment_id):
        reservation = self._reservation(reservation_id)
        user = self._user(reservation["user_id"])
        total_price = 0
        new_flights = []
        for fi in flights:
            match = next((rf for rf in reservation["flights"]
                          if rf["flight_number"] == fi["flight_number"] and rf["date"] == fi["date"]
                          and cabin == reservation["cabin"]), None)
            if match:
                total_price += match["price"] * len(reservation["passengers"])
                new_flights.append(match)
                continue
            flight = self._flight(fi["flight_number"])
            inst = self._flight_instance(fi["flight_number"], fi["date"])
            if inst.get("status") != "available":
                raise ToolError(f"Flight {fi['flight_number']} not available on date {fi['date']}")
            if inst.get("available_seats", {}).get(cabin, 0) < len(reservation["passengers"]):
                raise ToolError(f"Not enough seats on flight {fi['flight_number']}")
            rf = {"flight_number": fi["flight_number"], "date": fi["date"],
                  "price": inst["prices"][cabin], "origin": flight["origin"],
                  "destination": flight["destination"]}
            total_price += rf["price"] * len(reservation["passengers"])
            new_flights.append(rf)
        total_price -= sum(f["price"] for f in reservation["flights"]) * len(reservation["passengers"])
        payment = self._payment_for_update(user, payment_id, total_price)
        if payment is not None:
            reservation["payment_history"].append(payment)
        reservation["flights"] = new_flights
        reservation["cabin"] = cabin
        return reservation

    def update_reservation_baggages(self, reservation_id, total_baggages, nonfree_baggages, payment_id):
        reservation = self._reservation(reservation_id)
        user = self._user(reservation["user_id"])
        total_price = 50 * max(0, nonfree_baggages - reservation["nonfree_baggages"])
        payment = self._payment_for_update(user, payment_id, total_price)
        if payment is not None:
            reservation["payment_history"].append(payment)
        reservation["total_baggages"] = total_baggages
        reservation["nonfree_baggages"] = nonfree_baggages
        return reservation

    def update_reservation_passengers(self, reservation_id, passengers):
        reservation = self._reservation(reservation_id)
        if len(passengers) != len(reservation["passengers"]):
            raise ToolError("Number of passengers does not match")
        reservation["passengers"] = deepcopy(passengers)
        return reservation

    def cancel_reservation(self, reservation_id):
        reservation = self._reservation(reservation_id)
        refunds = [{"payment_id": p["payment_id"], "amount": -p["amount"]}
                   for p in reservation["payment_history"]]
        reservation["payment_history"].extend(refunds)
        reservation["status"] = "cancelled"
        return reservation

    def send_certificate(self, user_id, amount):
        user = self._user(user_id)
        for pid in [f"certificate_{i}" for i in self._new_payment_ids()]:
            if pid not in user["payment_methods"]:
                user["payment_methods"][pid] = {"id": pid, "amount": amount, "source": "certificate"}
                return f"Certificate {pid} added to user {user_id} with amount {amount}."
        raise ToolError("Too many certificates")

    # --- READ tools (non-mutating) ---------------------------------------------------------------
    def get_user_details(self, user_id):
        return self._user(user_id)

    def get_reservation_details(self, reservation_id):
        return self._reservation(reservation_id)

    def list_all_airports(self):
        airports = sorted({f["origin"] for f in self.db.flights.values()} |
                          {f["destination"] for f in self.db.flights.values()})
        return airports

    def get_flight_status(self, flight_number, date):
        return self._flight_instance(flight_number, date).get("status")

    def search_direct_flight(self, origin, destination, date):
        out = []
        for f in self.db.flights.values():
            if f["origin"] == origin and f["destination"] == destination:
                inst = f.get("dates", {}).get(date)
                if inst and inst.get("status") == "available":
                    out.append({"flight_number": f["flight_number"], "date": date, **inst})
        return out

    def search_onestop_flight(self, origin, destination, date):
        # Minimal: direct only (one-stop search not needed for the DB-state oracle).
        return self.search_direct_flight(origin, destination, date)

    def calculate(self, expression):
        allowed = set("0123456789+-*/(). ")
        if not set(expression) <= allowed:
            raise ToolError("Invalid characters in expression")
        return str(round(float(eval(expression, {"__builtins__": {}}, {})), 2))

    def transfer_to_human_agents(self, summary):
        return "Transfer successful"

    # --- dispatch --------------------------------------------------------------------------------
    def execute(self, name: str, arguments: dict):
        fn = getattr(self, name, None)
        if fn is None or name.startswith("_"):
            raise ToolError(f"Unknown tool {name}")
        return fn(**(arguments or {}))

"""
The prompt and JSON schema sent to the model.

The reading rules are the trial's strict "v2" prompt unchanged: on 100 hand-checked photos GPT-6 Sol
never misread a digit with it (13/13 readings had every digit right; its only errors were decimal points,
which the meter-type rules in this package now handle). Added on top: in step 1, tariff (T1/T2/TOD), billing-history and prepaid-balance screens named as not the total (they fooled the hand review), and the LCD label copied into screen_label so the service can check it (checks.py); step 6, copying the printed
make and model code so the service can look up the meter type and its decimal rule; and step 7, a best
guess (digits and decimal places) returned with every retake/review so a person always has a starting value.
The guesses are separate fields: they never change status or display_text, and are never billed on.
"""

PROMPT_VERSION = "deploy-4 (trial v2 + meter identification + best guess + tariff/prepaid/bill screens + screen label)"

PROMPT = """You are reading a photo of a digital (LCD) electricity meter in India. The reading will be used for billing.

A WRONG READING IS FAR WORSE THAN NO READING. UNREADABLE is always an acceptable answer. Answer READ only when you are certain of every single character; if you have any doubt at all, answer UNREADABLE.

Target: the CUMULATIVE ENERGY reading (total kWh, or kVAh on meters that bill in kVAh). It may be labelled kWh, kVAh, CUM, or have no label.

Work through these checks in order:

1. Screen. The display cycles through several screens. Only the cumulative-energy screen counts. If the LCD shows anything else (maximum demand kW / kVA / MD, voltage, current, power factor, frequency, date, time, meter ID or serial number, a test or all-segments-lit screen, a number with a label you do not recognise), or you cannot tell which screen it is, the status is NOT_CUMULATIVE_SCREEN. Three screens look like the total but are not: a tariff or time-of-day zone register (labelled T1, T2, T3..., TOD, TZ, ZONE or similar), which holds only part of the energy; a billing-history register (labelled BILL, 01 BILL, HIST, PREV, LAST or similar), which shows an earlier bill's reading; and a prepaid balance or money screen (INR, Rs, ₹, LTR, BAL, CREDIT). All three are NOT_CUMULATIVE_SCREEN. If no LCD is visible, or it is blank or off, the status is NO_DISPLAY.
   Copy every label, word, symbol or small number shown on the LCD around the main row (for example "CUM kWh", "kVAh", "T2 kVAh", "01 Bill", "MD kW") into screen_label exactly as shown; null if the LCD shows none.

2. Digits. Read the main row of the LCD one character at a time, left to right, including leading zeros. For each digit decide whether it is fully and unambiguously legible: no glare, blur, shadow, dirt, scratch, reflection or missing segment that could change it. Watch for the usual seven-segment confusions: 0/8, 3/8/9, 5/6, 1/7, 2/7. A digit that could be two different values is NOT legible.

3. Count. Make sure no digit is missing: none cut off at the left or right edge, hidden by glare or the frame, or too faint to see. If the number of digits is not certain, the digit count is not certain.

4. Decimal point. Include a decimal point in display_text ONLY if you can actually see one on the LCD. Never add one, never move one, never append ".0". Some meters show no decimal point at all.

5. Decide. status is READ only if: it is the cumulative-energy screen, every digit is legible, and the digit count is certain. Otherwise the status is UNREADABLE (or NOT_CUMULATIVE_SCREEN / NO_DISPLAY).

6. Meter identification. Separately from the LCD, copy the text printed on the meter body that identifies it: the manufacturer or brand (for example HPL, L&T, Schneider, Genus, Secure, Adani) and the model or type code (for example "PPSM 01", "ER300P", "EM101+"). Copy them exactly as printed, and put any other short identifying codes printed near the display (such as "WR300BC63ASSAM1") in faceplate_text. Use null or an empty list for anything you cannot see. Never guess.

7. Best guess. Separately, and without changing status or display_text, give your best guess of the main LCD row so that a person checking the photo has a starting value. These guesses never make an uncertain reading certain: status READ still requires every check above.
   - best_guess: display_text with every "?" replaced by the digit you think most likely. Keep a decimal point only where display_text has one. null only if there is no display.
   - decimal_places_guess: how many digits you think follow the decimal point on this display. Look for a small dot, a last digit that is smaller, boxed, underlined or set apart, or a decimal marker printed on the LCD. 0 if you think the display shows no decimal digit; null if you cannot tell.

Ignore every number printed on the meter body or labels when reading the display: serial number, barcode, IMEI, ratings, dates.

Fields:
- digits: each character of the main LCD row in order (a digit, or "." for a visible decimal point), with legible true/false. Empty if there is no display.
- display_text: the main LCD row exactly as shown (digits, plus a decimal point only if visible). Fill it even when UNREADABLE, as your best guess, with "?" for any digit you cannot read. null if there is no display.
- confidence: your probability (0 to 1) that display_text is exactly right, character for character.
- reason: one short sentence."""

SCREEN_TYPES = ["cumulative_energy", "max_demand", "voltage_current_pf", "date_time", "meter_id",
                "test_or_segment_check", "other_register", "blank_or_off", "not_visible", "other"]
STATUSES = ["READ", "UNREADABLE", "NOT_CUMULATIVE_SCREEN", "NO_DISPLAY"]

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["screen_type", "unit", "digits", "digit_count_certain", "decimal_point_visible", "display_text",
                 "meter_make", "meter_model_code", "faceplate_text", "status", "confidence", "reason",
                 "best_guess", "decimal_places_guess", "screen_label"],
    "properties": {
        "screen_type": {"type": "string", "enum": SCREEN_TYPES},
        "unit": {"type": ["string", "null"]},
        "digits": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["char", "legible"],
            "properties": {"char": {"type": "string"}, "legible": {"type": "boolean"}}}},
        "digit_count_certain": {"type": "boolean"},
        "decimal_point_visible": {"type": "boolean"},
        "display_text": {"type": ["string", "null"]},
        "meter_make": {"type": ["string", "null"]},
        "meter_model_code": {"type": ["string", "null"]},
        "faceplate_text": {"type": "array", "items": {"type": "string"}},
        "status": {"type": "string", "enum": STATUSES},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
        "best_guess": {"type": ["string", "null"]},
        "decimal_places_guess": {"type": ["integer", "null"]},
        "screen_label": {"type": ["string", "null"]},
    },
}

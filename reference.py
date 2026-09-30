"""Reference colour chart for a standard 10-parameter urine strip.

Pad order is from the TIP of the strip (pad 1) to the HANDLE (pad 10).
Each level = (label, numeric value used for trends, RGB colour on the chart).
These colours approximate a typical chart; replace them with the chart
printed on your own strip bottle for a real calibration.
"""

CHART = {
    "Glucose": [("Negative", 0, (115, 200, 200)), ("100 mg/dL", 100, (140, 200, 160)),
                ("250 mg/dL", 250, (150, 170, 90)), ("500 mg/dL", 500, (140, 130, 60)),
                ("1000 mg/dL", 1000, (130, 95, 50)), ("2000 mg/dL", 2000, (110, 70, 40))],
    "Bilirubin": [("Negative", 0, (245, 230, 190)), ("Small +", 1, (230, 200, 170)),
                  ("Moderate ++", 2, (210, 175, 160)), ("Large +++", 3, (190, 150, 150))],
    "Ketones": [("Negative", 0, (235, 200, 180)), ("Trace 5", 5, (230, 170, 170)),
                ("15 mg/dL", 15, (215, 130, 150)), ("40 mg/dL", 40, (190, 90, 130)),
                ("80 mg/dL", 80, (160, 60, 110)), ("160 mg/dL", 160, (120, 40, 90))],
    "Specific Gravity": [("1.000", 1.000, (40, 90, 100)), ("1.005", 1.005, (60, 110, 90)),
                         ("1.010", 1.010, (95, 125, 80)), ("1.015", 1.015, (125, 135, 70)),
                         ("1.020", 1.020, (150, 145, 60)), ("1.025", 1.025, (170, 155, 55)),
                         ("1.030", 1.030, (190, 165, 50))],
    "Blood": [("Negative", 0, (240, 190, 80)), ("Trace", 0.5, (200, 190, 80)),
              ("Small +", 1, (150, 170, 80)), ("Moderate ++", 2, (90, 140, 80)),
              ("Large +++", 3, (40, 90, 60))],
    "pH": [("5.0", 5.0, (240, 130, 60)), ("6.0", 6.0, (215, 170, 60)), ("6.5", 6.5, (180, 180, 70)),
           ("7.0", 7.0, (140, 170, 70)), ("7.5", 7.5, (100, 160, 90)), ("8.0", 8.0, (60, 140, 110)),
           ("8.5", 8.5, (30, 110, 130))],
    "Protein": [("Negative", 0, (220, 230, 120)), ("Trace", 15, (190, 215, 120)),
                ("30 mg/dL", 30, (160, 200, 120)), ("100 mg/dL", 100, (120, 180, 120)),
                ("300 mg/dL", 300, (90, 160, 120)), ("2000 mg/dL", 2000, (60, 130, 110))],
    "Urobilinogen": [("0.2 mg/dL", 0.2, (245, 200, 160)), ("1 mg/dL", 1, (240, 180, 150)),
                     ("2 mg/dL", 2, (235, 160, 140)), ("4 mg/dL", 4, (225, 140, 130)),
                     ("8 mg/dL", 8, (210, 110, 110))],
    "Nitrite": [("Negative", 0, (250, 240, 230)), ("Positive", 1, (240, 190, 200))],
    "Leukocytes": [("Negative", 0, (240, 235, 210)), ("Trace", 0.5, (225, 215, 210)),
                   ("Small +", 1, (200, 185, 200)), ("Moderate ++", 2, (175, 150, 190)),
                   ("Large +++", 3, (140, 110, 170))],
}

# name -> (unit, normal_min, normal_max, plain-English description)
ANALYTE_INFO = {
    "Glucose": ("mg/dL", 0, 0, "Sugar in urine. Usually absent; high levels can point to diabetes."),
    "Bilirubin": ("", 0, 0, "Breakdown product of red blood cells. Presence can suggest a liver problem."),
    "Ketones": ("mg/dL", 0, 0, "Made when the body burns fat. Seen in fasting, keto diets or uncontrolled diabetes."),
    "Specific Gravity": ("", 1.005, 1.030, "How concentrated the urine is. Low = very dilute, high = dehydrated."),
    "Blood": ("", 0, 0, "Red blood cells / haemoglobin. Can come from infection, stones or menstruation."),
    "pH": ("", 5.0, 8.0, "Acidity of urine. Normal range is about 5 to 8."),
    "Protein": ("mg/dL", 0, 0, "Protein should be mostly absent. Persistent protein may mean kidney stress."),
    "Urobilinogen": ("mg/dL", 0.2, 1.0, "Normal in small amounts; raised in some liver conditions."),
    "Nitrite": ("", 0, 0, "Produced by some bacteria. Positive often means a urinary tract infection."),
    "Leukocytes": ("", 0, 0, "White blood cells. Raised levels are a common sign of infection or inflammation."),
}

PAD_ORDER = list(CHART.keys())


# Tests whose chart levels are real numbers, so an approximate value can be shown
# (name -> number of decimals to show). The others (+, ++, Positive) are shown as levels only.
MEASURED = {"Glucose": 0, "Ketones": 0, "Specific Gravity": 3, "pH": 1, "Protein": 0, "Urobilinogen": 1}

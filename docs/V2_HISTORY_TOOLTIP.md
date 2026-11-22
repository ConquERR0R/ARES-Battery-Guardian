# Battery History hover tooltip (V2 Beta)

## What it does

Move the mouse over the chart on the **History** page. The chart then:

- picks the recorded point nearest in time to the mouse,
- draws a dashed vertical guide line and a small round marker on that point,
- shows a box next to the marker with:
  - the exact battery percentage, as stored (for example `87%` or `87.5%`),
  - the date and time of the reading (`YYYY-MM-DD HH:MM:SS`, local time),
  - the state at that reading: **Charging**, **Discharging**, **On AC, not charging**, or **Unknown**
    when the sample has no state recorded,
  - **Voltage**, only when the sample has a voltage above 0,
  - **Capacity** (`remaining of full-charge Wh`), only when recorded. If only one of the two values
    was recorded, it shows that one. Values that are 0, negative or not numbers are left out.

The box disappears when the mouse leaves the chart or moves over the axis labels. It works the
same for every range (Last 6 hours, 24 hours, 7 days, 30 days). The Health chart gets the same
hover, showing the health % and the day.

## How it is built

- The chart is the existing lightweight `LineChart` (QPainter) in `app/ui/dashboard.py`. ARES does
  not use QtCharts, pyqtgraph or matplotlib, so there is no plotting library with a built-in
  tooltip. The hover is added to that widget; the chart was not replaced.
- Qt's own `QToolTip` pop-up was not used, because it re-opens and repositions as the mouse
  moves, which flickers. The chart paints the box itself instead, using the theme's tooltip
  colours (`QPalette.ToolTipBase` and `ToolTipText`), so it is readable in both dark and light
  themes.
- No flicker: the chart repaints only when the nearest point changes, not on every mouse move.
- When the chart refreshes with new samples while the mouse is over it, the tooltip moves to the
  point now nearest the mouse.
- The box is flipped to the left of the marker and kept inside the chart near the edges.
- The nearest point is found with a binary search, so 30 days of samples stay fast.

## What did not change

- Line colour, charging shading, threshold lines, axes, layout and range choices are unchanged.
- The chart has no zoom feature (before or now), so there was no zoom to preserve.
- The database and history records are only read. Samples without a percentage are skipped on the
  chart, as before. Health days without a value are now skipped too, instead of failing to draw.

## Tests

`tests/test_history_tooltip.py`, 17 tests (offscreen Qt):

- tooltip text: percentage and time; exact decimals; every charging state; voltage and capacity
  only when present; partial and invalid readings; missing columns;
- nearest-point search, including before the first and after the last point;
- mouse interaction: nearest point selected; no repaint while on the same point; hidden on leave
  and over the axis; fewer than two points; data and details lists of different lengths; refresh
  while hovering; the box stays inside the widget;
- the marker and box are painted in both themes, with the theme's tooltip colours and readable
  contrast;
- a 30-day database with incomplete samples: every range shows tooltips, and no history record
  changes.

## Limitations

- Tested in an offscreen Qt environment on Linux. It still needs a check by hand on Windows 11
  with a real mouse.
- Voltage and capacity show only for samples where the Windows battery driver reported them. On
  many laptops some readings have neither.
- On a touch screen there is no hover, so the tooltip appears only with a mouse or touchpad.

# GPIO circuit trace rules
<!-- py fallback only: loaded by susi_gen.py when the agent did not write the artifact.
     The agent's rules are AnalysisSKill/bios_circuit_image_analysis_rule.md R-016.
     This file is the English copy of those rules; change both together. -->

## Scope: build the target signal set first
- Trace only the external GPIO signals of the target pattern given in the case info (for example `EC_P*_GPIO*` or `SIO_GPIO*`).
- List the complete target signal set and its count before mapping. Every member must end with a mapping or an AMBIGUOUS result.
- For `EC_P*_GPIO*`, include every port (P1, P2, P3, ...). Never stop after the first port group.
- Signals outside the target pattern are OUT_OF_SCOPE even when they reach a GP* pin (for example FAN_SPEED*, *_PWM, *BEEP*, SIO_LED*, PORT80*). Do not output them.

## Reading PDF schematics and small text
- Locate first: search the PDF text for the target pattern or keywords and list every matching page and region. Do not stop at the first hit or the first port group.
- Zoom before judging: render each hit region as a focused high-resolution crop in which signal labels, wire bends, junction dots, pin numbers and function labels are clearly readable.
- A full-page render is for navigation only. Never accept a final mapping from an image whose text is too small to read; make a tighter, higher-zoom crop first, and mark AMBIGUOUS only if it is still unreadable.
- Keep the evidence: save the crops in the project directory (not only /tmp) and list them in `meta.evidence_images`.

## Tracing method
- The external signal name is only the starting point. The answer is the GPIO function label printed next to the chip pin that the wire actually reaches.
- Follow the continuous electrical wire segment by segment, including bends and vertical runs. Text position, row alignment, OCR order and name similarity are never connection evidence.
- The most common error is landing on the adjacent pin (for example `GPIOA6` instead of `GPIOA5`, `GPIO91` instead of `GPIO90`). Before accepting a mapping, confirm the pin number printed at the exact wire end and read the function label on that same pin row.
- A crossing without a junction dot is not a connection. Branch only at junctions.
- An `X`/`NC` mark counts only when it sits on the same pin or wire end and the wire terminates there.
- A `<number>` tag next to a net label (for example `EC_P1_GPIO2 <49>`) does not change the judgement. If a continuous wire on the same image reaches a chip pin, map it normally; if no wire reaches a chip pin, mark AMBIGUOUS.
- The `<number>` tag only lists other pages that use the same net; it never means the wire is missing on this page. Follow the wire from the port/BI symbol on the side opposite the tag (it may run left toward the chip) and through series 0Ω resistors/jumpers to the chip pin.
- When the wire is cut off, the end point is unclear, or a wire cannot be told apart from an annotation, mark AMBIGUOUS. Never guess.

## Function label to group/bit
- `GPxy` / `GPIOxy` -> group x, bit y (for example `GPIO34` -> 3,4; `GP50` -> 5,0).
- The group digit may be hexadecimal: `GPIOA5` -> 10,5; `GPIOB0` -> 11,0; `GPIOD0` -> 13,0.
- Decode group/bit only from the chip function label. Never derive them from the external signal suffix (`EC_P2_GPIO5` does not mean group 2 or bit 5).
- The chip package pin (for example `F1`, `L6`) is supporting evidence only; record it when readable.
- Do not decide INI keys. The generator numbers keys `GPIO00`, `GPIO01`, ... in signal order (`EC_P1_GPIO0..7`, then `EC_P2_GPIO0..7`, ...; `SIO_GPIOn` by n). Put the chip function label in `function_label`, never in `report_name`.

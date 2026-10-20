# Cura Post Processing Script
# FlashforgeDreamer.py
#
# Cura 5.14
#
# FlashForge Dreamer:
#   T0 = RIGHT nozzle
#   T1 = LEFT nozzle
#
# Dreamer uses:
#   M108 T0
#   M108 T1
#
# This script:
#   1. Tracks active extruder from M108 T0/T1
#   2. Adds T0/T1 to M104 commands when missing
#   3. Converts Cura M109 temperature commands to Dreamer M104 + M6
#       and M190 temperature commands to Dreamer M140 + M7
#   4. Removes M105 commands
#   5. Removes standalone T0/T1 tool-change commands
#   6. Moves Cura's marked purge block after the first M907 and assigns one
#      of its two equal purge strokes to each nozzle used in the print
#   7. Moves endcode unload moves after the Y-min/X0 park and before nozzle
#      shutdown, repeating them for each nozzle used in the print
#      Moves an existing endcode unload block after the Y-min/X0 park and
#      before nozzle shutdown; it leaves the file alone if no block exists
#
# M108 T0/T1 commands are preserved.

from ..Script import Script
import re


class FlashforgeDreamer(Script):

    def getSettingDataString(self):
        return """{
            "name": "FlashForge Dreamer",
            "key": "FlashforgeDreamer",
            "metadata": {},
            "version": 2,
            "settings":
            {
                "enabled":
                {
                    "label": "Enable Dreamer G-code modification",
                    "description": "Adapts Cura G-code for FlashForge Dreamer.",
                    "type": "bool",
                    "default_value": true
                }
            }
        }"""

    def _get_used_extruders(self, data):
        """Get nozzles named by M6 commands before the first print layer.

        If startup has no M6 T0/T1 command, use the last M108 selection before
        the first layer, or T0 if there is no selection.
        """
        used_extruders = set()
        active_extruder = 0

        for section in data:
            for line in section.split("\n"):
                stripped = line.strip()
                if re.match(r"^;\s*LAYER:", stripped, re.IGNORECASE):
                    return used_extruders or {active_extruder}

                m6_match = re.match(
                    r"^M6\s+T([01])(?:\s|;|$)", stripped, re.IGNORECASE
                )
                if m6_match:
                    used_extruders.add(int(m6_match.group(1)))

                m108_match = re.match(
                    r"^M108\s+T([01])(?:\s|;|$)", stripped, re.IGNORECASE
                )
                if m108_match:
                    active_extruder = int(m108_match.group(1))

        return used_extruders or {active_extruder}

    def _move_purge_after_m907(self, data, used_extruders):
        """Move the marked purge block after M907 and replay it per M6 tool.

        PurgeLinesAndUnload supplies the exact start and end comments. The
        whole marked sequence is retained; its X/Y moves are not inspected.
        """
        purge_start = ";---------------------[Purge Lines]"
        purge_end = ";---------------------[End of Purge]"
        sections = [section.split("\n") for section in data]
        flattened = [line for lines in sections for line in lines]

        start_positions = [
            index for index, line in enumerate(flattened)
            if line.strip().casefold() == purge_start.casefold()
        ]
        end_positions = [
            index for index, line in enumerate(flattened)
            if line.strip().casefold() == purge_end.casefold()
        ]
        if (len(start_positions) != 1 or len(end_positions) != 1
                or end_positions[0] <= start_positions[0]):
            return data

        start_position = start_positions[0]
        end_position = end_positions[0]
        purge_body = flattened[start_position + 1:end_position]
        purge_order = [tool for tool in (1, 0) if tool in used_extruders]
        if not purge_order:
            return data

        rebuilt_sections = [[] for _ in sections]
        flat_position = 0
        for section_index, lines in enumerate(sections):
            for line in lines:
                if not start_position <= flat_position <= end_position:
                    rebuilt_sections[section_index].append(line)
                flat_position += 1

        purge_sequence = [purge_start, "M82 ; Absolute extrusion mode for purge"]
        for tool in purge_order:
            purge_sequence.extend([
                "; [FlashforgeDreamer] Purge nozzle T{}".format(tool),
                "M108 T{}".format(tool),
                "G92 E0 ; Reset this nozzle's extruder",
            ])
            purge_sequence.extend(purge_body)
        purge_sequence.extend([
            "G92 E0 ; Reset extruder after purge",
            purge_end,
        ])

        m907_pattern = re.compile(r"^\s*M907(?:\s|;|$)", re.IGNORECASE)
        for lines in rebuilt_sections:
            for line_index, line in enumerate(lines):
                if m907_pattern.match(line):
                    lines[line_index + 1:line_index + 1] = purge_sequence
                    return ["\n".join(section) for section in rebuilt_sections]
        return data

    def _duplicate_unload_for_used_extruders(self, data, used_extruders):
        """Repeat Cura's relative unload moves for each startup M6 tool."""
        tool_order = [tool for tool in (1, 0) if tool in used_extruders]
        if not tool_order:
            return data

        endcode_pattern = re.compile(r"^\s*;\s*endcode\b", re.IGNORECASE)
        m83_pattern = re.compile(r"^\s*M83(?:\s|;|$)", re.IGNORECASE)
        m82_pattern = re.compile(r"^\s*M82(?:\s|;|$)", re.IGNORECASE)
        m400_pattern = re.compile(r"^\s*M400(?:\s|;|$)", re.IGNORECASE)
        m104_pattern = re.compile(r"^\s*M104\b", re.IGNORECASE)
        g92_e_pattern = re.compile(
            r"^\s*G92\b(?=.*(?:^|\s)E0(?:\.0+)?(?:\s|$))",
            re.IGNORECASE
        )
        g1_e_pattern = re.compile(
            r"^\s*G1\b(?=.*(?:^|\s)E-?\d+(?:\.\d*)?(?:\s|$))",
            re.IGNORECASE
        )
        unload_marker_pattern = re.compile(
            r"^\s*;\s*\[FlashforgeDreamer\]\s*Unload nozzle T[01]\b",
            re.IGNORECASE
        )

        def is_nozzle_off(line):
            code = line.partition(";")[0].strip()
            if not m104_pattern.match(code):
                return False
            target = re.search(
                r"(?:^|\s)S(-?\d+(?:\.\d*)?)(?:\s|$)",
                code,
                re.IGNORECASE
            )
            return target is not None and float(target.group(1)) == 0

        def is_y_min_home(line):
            code = line.partition(";")[0].strip()
            return (
                re.match(r"^G161\b", code, re.IGNORECASE) is not None
                and re.search(r"(?:^|\s)Y(?:\s|$)", code, re.IGNORECASE) is not None
            )

        def is_x_zero_move(line):
            code = line.partition(";")[0].strip()
            return (
                re.match(r"^G[01]\b", code, re.IGNORECASE) is not None
                and re.search(
                    r"(?:^|\s)X[+-]?0(?:\.0+)?(?=\s|$)", code, re.IGNORECASE
                ) is not None
            )

        for section_index, section in enumerate(data):
            lines = section.split("\n")
            endcode_indices = [
                index for index, line in enumerate(lines)
                if endcode_pattern.match(line)
            ]
            if len(endcode_indices) != 1:
                continue
            endcode_index = endcode_indices[0]

            m83_index = next(
                (index for index in range(endcode_index + 1, len(lines))
                 if m83_pattern.match(lines[index])
                 and "unload" in lines[index].casefold()),
                None
            )
            if m83_index is None:
                continue
            if any(unload_marker_pattern.match(line)
                   for line in lines[m83_index + 1:]):
                return data

            m82_index = next(
                (index for index in range(m83_index + 1, len(lines))
                 if m82_pattern.match(lines[index])),
                None
            )
            if m82_index is None:
                continue

            unload_moves = [
                lines[index]
                for index in range(m83_index + 1, m82_index)
                if g1_e_pattern.match(lines[index].partition(";")[0])
            ]
            if not unload_moves:
                continue

            valid_commands = True
            for line in lines[m83_index + 1:m82_index]:
                code = line.partition(";")[0].strip()
                if (not code or m400_pattern.match(code)
                        or g1_e_pattern.match(code) or g92_e_pattern.match(code)
                        or is_nozzle_off(line)):
                    continue
                valid_commands = False
                break
            if not valid_commands:
                continue

            y_home_indices = [
                index for index in range(endcode_index + 1, len(lines))
                if is_y_min_home(lines[index])
            ]
            if not y_home_indices:
                continue
            x_zero_indices = [
                index for index in range(y_home_indices[-1] + 1, len(lines))
                if is_x_zero_move(lines[index])
            ]
            if not x_zero_indices:
                continue
            insert_index = x_zero_indices[-1] + 1

            shutdown_indices = [
                index for index in range(endcode_index + 1, len(lines))
                if is_nozzle_off(lines[index])
            ]
            if shutdown_indices and insert_index > shutdown_indices[0]:
                continue

            reset_lines = []
            reset_indices = set()
            for index in range(m82_index + 1, len(lines)):
                if is_nozzle_off(lines[index]):
                    break
                if g92_e_pattern.match(lines[index].partition(";")[0]):
                    reset_lines.append(lines[index])
                    reset_indices.add(index)
            if not reset_lines:
                reset_lines = ["G92 E0 ; Reset extruder position"]

            moved_block = ["M400 ; Finish parking move", lines[m83_index]]
            for tool in tool_order:
                moved_block.extend([
                    "; [FlashforgeDreamer] Unload nozzle T{}".format(tool),
                    "M108 T{}".format(tool),
                ])
                moved_block.extend(unload_moves)
                moved_block.append("M400 ; Finish unload of T{}".format(tool))
            moved_block.extend([lines[m82_index], *reset_lines])

            remove_indices = {m83_index, m82_index, *reset_indices}
            remove_indices.update(
                index for index in range(m83_index + 1, m82_index)
                if (g1_e_pattern.match(lines[index].partition(";")[0])
                    or m400_pattern.match(lines[index].partition(";")[0]))
            )

            rebuilt = []
            inserted = False
            for index, line in enumerate(lines):
                if index == insert_index:
                    rebuilt.extend(moved_block)
                    inserted = True
                if index not in remove_indices:
                    rebuilt.append(line)
            if not inserted:
                rebuilt.extend(moved_block)

            data[section_index] = "\n".join(rebuilt)
            return data

        return data

    def execute(self, data):

        if not self.getSettingValueByKey("enabled"):
            return data

        used_extruders = self._get_used_extruders(data)
        data = self._move_purge_after_m907(data, used_extruders)
        data = self._duplicate_unload_for_used_extruders(data, used_extruders)

        # Dreamer starts with T0 as the default assumption.
        active_extruder = 0

        for layer_index in range(len(data)):

            lines = data[layer_index].split("\n")
            output = []

            for line in lines:

                stripped = line.strip()

                # ---------------------------------------------------------
                # M108 T0 / M108 T1
                #
                # Track the active Dreamer extruder.
                # Keep the M108 line in the G-code.
                # ---------------------------------------------------------

                m108_match = re.match(
                    r"^\s*M108\s+T([01])(?:\s|;|$)",
                    stripped,
                    re.IGNORECASE
                )

                if m108_match:
                    active_extruder = int(m108_match.group(1))
                    output.append(line)
                    continue

                # ---------------------------------------------------------
                # Remove M105
                # ---------------------------------------------------------

                if re.match(
                    r"^\s*M105(?:\s|;|$)",
                    stripped,
                    re.IGNORECASE
                ):
                    continue

                # ---------------------------------------------------------
                # Convert Cura/Marlin M109 temperature waits to Dreamer form.
                # M104 sets the target; M6 waits for that nozzle. M6's optional
                # S parameter is a timeout, so Cura's temperature S value must
                # not be copied to M6.
                # ---------------------------------------------------------

                m109_code = line.partition(";")[0].strip()
                m109_match = re.match(r"^M109\b(.*)$", m109_code, re.IGNORECASE)
                if m109_match:
                    arguments = m109_match.group(1)
                    target_match = re.search(
                        r"(?:^|\s)S(-?\d+(?:\.\d+)?)",
                        arguments,
                        re.IGNORECASE
                    )
                    tool_match = re.search(
                        r"(?:^|\s)T([01])(?:\s|$)",
                        arguments,
                        re.IGNORECASE
                    )

                    # M109 S... is a temperature command in Cura's Marlin
                    # output. Dreamer's M104 sets it, then M6 waits. A
                    # non-temperature M109 (for example IDEX mode selection)
                    # is not meaningful on the Dreamer and is dropped.
                    if target_match:
                        tool = (
                            int(tool_match.group(1))
                            if tool_match else active_extruder
                        )
                        output.append(
                            "M104 S{} T{}".format(target_match.group(1), tool)
                        )
                        output.append("M6 T{} S30".format(tool))
                    continue
                    
                # ---------------------------------------------------------
                # Convert Cura/Marlin M190 bed waits to Dreamer form.
                # M140 sets the bed target; M7 waits for the bed to reach it.
                # Dreamer's bed is addressed as T0 in its Cura setup G-code.
                # ---------------------------------------------------------

                m190_code = line.partition(";")[0].strip()
                m190_match = re.match(r"^M190\b(.*)$", m190_code, re.IGNORECASE)
                if m190_match:
                    arguments = m190_match.group(1)
                    target_match = re.search(
                        r"(?:^|\s)[SR](-?\d+(?:\.\d+)?)",
                        arguments,
                        re.IGNORECASE
                    )
                    if target_match:
                        output.append(
                            "M140 S{} T0".format(target_match.group(1))
                        )
                        output.append("M7 T0 S60")
                    continue

                # ---------------------------------------------------------
                # Remove standalone T0 / T1 tool changes.
                #
                # Do NOT remove M108 T0 / M108 T1 here, because M108
                # was already handled above.
                # ---------------------------------------------------------

                if re.match(
                    r"^\s*T[01](?:\s*;.*)?$",
                    stripped,
                    re.IGNORECASE
                ):
                    continue

                # ---------------------------------------------------------
                # M104
                #
                # Add T0/T1 if the M104 does not already contain T0/T1.
                # ---------------------------------------------------------

                temp_match = re.match(
                    r"^(\s*)(M104)(\s+.*?)(\s*;.*)?$",
                    line,
                    re.IGNORECASE
                )

                if temp_match:

                    prefix = temp_match.group(1)
                    command = temp_match.group(2)
                    arguments = temp_match.group(3)
                    comment = temp_match.group(4) or ""

                    # Check whether M104 already has T0/T1.
                    has_tool = re.search(
                        r"(?:^|\s)T[01](?=\s|$)",
                        arguments,
                        re.IGNORECASE
                    )

                    if not has_tool:

                        arguments = arguments.rstrip()

                        line = (
                            prefix
                            + command
                            + arguments
                            + " T"
                            + str(active_extruder)
                            + comment
                        )

                output.append(line)

            data[layer_index] = "\n".join(output)

        return data
import re

with open("palimpsest/tasks/features.py", "r") as f:
    content = f.read()

# Replace line_offsets generation
new_line_offsets = """        import bisect
        
        line_offsets = []
        line_starts = []
        current_offset = 0
        for line in page_lines:
            l_len = len(line["text"])
            line_offsets.append((current_offset, current_offset + l_len, line["bbox"]))
            line_starts.append(current_offset)
            current_offset += l_len + 1  # +1 for newline

        def get_bbox_union(c_start: int, c_end: int) -> list[float]:
            idx = bisect.bisect_right(line_starts, c_start) - 1
            if idx < 0:
                idx = 0
            
            x0, y0, x1, y1 = 1.0, 1.0, 0.0, 0.0
            found = False
            for i in range(idx, len(line_offsets)):
                l_start, l_end, b = line_offsets[i]
                if l_start <= c_end and l_end >= c_start:
                    found = True
                    x0 = min(x0, b[0])
                    y0 = min(y0, b[1])
                    x1 = max(x1, b[2])
                    y1 = max(y1, b[3])
                if l_start > c_end:
                    break
            if found:
                return [x0, y0, x1, y1]
            return [0.0, 0.0, 1.0, 1.0]"""

content = re.sub(r'        line_offsets = \[\]\n        current_offset = 0\n        for line in page_lines:\n            l_len = len\(line\["text"\]\)\n            line_offsets\.append\(\(current_offset, current_offset \+ l_len, line\["bbox"\]\)\)\n            current_offset \+= l_len \+ 1  # \+1 for newline', new_line_offsets, content)

# Replace all occurrences of linear bbox search with get_bbox_union
# Pattern:
#                bbox = [0.0, 0.0, 1.0, 1.0]
#                for start, end, b in line_offsets:
#                    if start <= char_start <= end:
#                        bbox = b
#                        break

content = re.sub(
    r'                bbox = \[0\.0, 0\.0, 1\.0, 1\.0\]\n                for start, end, b in line_offsets:\n                    if start <= char_start <= end:\n                        bbox = b\n                        break',
    r'                bbox = get_bbox_union(char_start, char_end)',
    content
)

# Replace the one nested deeper
content = re.sub(
    r'                        bbox = \[0\.0, 0\.0, 1\.0, 1\.0\]\n                        for start, end, b in line_offsets:\n                            if start <= char_start <= end:\n                                bbox = b\n                                break',
    r'                        bbox = get_bbox_union(char_start, char_end)',
    content
)

with open("palimpsest/tasks/features.py", "w") as f:
    f.write(content)

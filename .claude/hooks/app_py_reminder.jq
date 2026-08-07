if (((.tool_input.file_path // .tool_response.filePath // "") | gsub("\\\\"; "/") | split("/") | last) == "app.py")
then {
  hookSpecificOutput: {
    hookEventName: "PostToolUse",
    additionalContext: "Reminder: if this edit to app.py fixed a parser bug (is_section_header / _consume_leading_meta / _split_merged_label_tag / normalize_br_lines / process_block or similar section-boundary or meta-line extraction logic), before finishing: add a numbered entry to memory file project_parsing_fixes.md (existing #1-#35 format), consider a repro case in test_dublyor_corpus.py, and update the MEMORY.md index line + bug count. If this edit was unrelated to parser bugs, ignore this reminder."
  }
}
else empty
end

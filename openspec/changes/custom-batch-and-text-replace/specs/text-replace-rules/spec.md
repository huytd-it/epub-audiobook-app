## ADDED Requirements

### Requirement: Per-book text replace rules
The system SHALL allow the user to manage a list of `(find, replace, is_regex, position)` rules per book via a CRUD UI and matching JSON endpoints. Rules MUST be applied to chapter text in `position` order (ascending) when generating patch text for TTS, AND MUST NOT mutate the original `chapter.text` in the database.

#### Scenario: Create a simple replacement rule
- **WHEN** the user POSTs `/books/{book_id}/replace-rules` with `{"find": "AI", "replace": "A.I.", "is_regex": false, "position": 0}`
- **THEN** the rule is persisted and a 201 response with the new rule is returned

#### Scenario: Apply rules in order when computing patch text
- **WHEN** patch text is generated for chapter text "The AI uses AI internally" and there are two rules: `[("AI", "A.I.", false, 0), ("A.I.", "Artificial Intelligence", false, 1)]`
- **THEN** the resulting text is "The Artificial Intelligence uses Artificial Intelligence internally"

#### Scenario: Rule list update invalidates affected patches
- **WHEN** the user adds, edits, or removes a rule
- **THEN** every patch for that book whose `status = done` is reset to `pending` with `error_message = NULL` and `audio_path = NULL`, and the book's `final_audio_path` is cleared

#### Scenario: Original chapter text unchanged
- **WHEN** the user has rules that would replace "AI" in the chapter text
- **THEN** `chapter.text` in the database still contains "AI" (rules apply only at patch-text-build time)

### Requirement: Replace rule validation
The system MUST reject regex rules that fail to compile and return 400 with the compile error message. Empty `find` strings MUST be rejected. `position` MUST be a non-negative integer; ties are allowed (rules with equal position are applied in insertion order).

#### Scenario: Invalid regex rejected
- **WHEN** the user POSTs a rule with `is_regex=true` and `find="[invalid"`
- **THEN** the API returns 400 with the regex compile error and no rule is persisted

#### Scenario: Empty find rejected
- **WHEN** the user POSTs a rule with `find=""`
- **THEN** the API returns 400 with a clear validation message

### Requirement: Replace rules UI section
The system SHALL render a "Text replace rules" section on the book detail page with a form to add a new rule and a table of existing rules (find → replace, is_regex flag, position, delete button). Editing a rule's fields and saving SHALL submit a PUT to the rule's endpoint.

#### Scenario: Add rule from UI
- **WHEN** the user fills the add-rule form with `find="API"`, `replace="A.P.I."`, `is_regex=off`, `position=0` and clicks Add
- **THEN** the page reloads with the new rule appended to the table

#### Scenario: Delete rule
- **WHEN** the user clicks the delete button next to a rule
- **THEN** the rule is removed via DELETE endpoint and the page reloads without it

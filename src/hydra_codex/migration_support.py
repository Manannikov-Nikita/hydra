"""Migration statements kept separate from the storage facade."""

V2_TRIGGER_STATEMENTS = (
    """CREATE TRIGGER IF NOT EXISTS annotations_project_matches_session_insert
        BEFORE INSERT ON annotations FOR EACH ROW
        WHEN COALESCE((SELECT project_id FROM sessions WHERE session_id=NEW.session_id),'') != NEW.project_id
        BEGIN SELECT RAISE(ABORT, 'annotation project must match session project'); END""",
    """CREATE TRIGGER IF NOT EXISTS annotations_turn_matches_session_insert
        BEFORE INSERT ON annotations FOR EACH ROW
        WHEN COALESCE((SELECT session_id FROM turns WHERE turn_id=NEW.turn_id),'') != NEW.session_id
        BEGIN SELECT RAISE(ABORT, 'annotation turn must belong to session'); END""",
    """CREATE TRIGGER IF NOT EXISTS annotations_project_matches_session_update
        BEFORE UPDATE OF project_id,session_id ON annotations FOR EACH ROW
        WHEN COALESCE((SELECT project_id FROM sessions WHERE session_id=NEW.session_id),'') != NEW.project_id
        BEGIN SELECT RAISE(ABORT, 'annotation project must match session project'); END""",
    """CREATE TRIGGER IF NOT EXISTS annotations_turn_matches_session_update
        BEFORE UPDATE OF session_id,turn_id ON annotations FOR EACH ROW
        WHEN COALESCE((SELECT session_id FROM turns WHERE turn_id=NEW.turn_id),'') != NEW.session_id
        BEGIN SELECT RAISE(ABORT, 'annotation turn must belong to session'); END""",
)

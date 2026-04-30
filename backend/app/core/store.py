from app.core.models import BasicReport, ScriptDocument, ScriptSegment


class ProjectStore:
    def __init__(self) -> None:
        self._scripts: dict[str, ScriptDocument] = {}
        self._segments: dict[str, list[ScriptSegment]] = {}
        self._reports: dict[str, BasicReport] = {}

    def save_script(self, script: ScriptDocument, segments: list[ScriptSegment]) -> None:
        self._scripts[script.id] = script
        self._segments[script.id] = segments

    def get_script(self, script_id: str) -> ScriptDocument:
        try:
            return self._scripts[script_id]
        except KeyError as error:
            raise KeyError(f"Script not found: {script_id}") from error

    def get_segments(self, script_id: str) -> list[ScriptSegment]:
        try:
            return self._segments[script_id]
        except KeyError as error:
            raise KeyError(f"Segments not found for script: {script_id}") from error

    def save_report(self, report: BasicReport) -> None:
        self._reports[report.script_id] = report

    def get_report(self, script_id: str) -> BasicReport:
        try:
            return self._reports[script_id]
        except KeyError as error:
            raise KeyError(f"Report not found for script: {script_id}") from error


store = ProjectStore()

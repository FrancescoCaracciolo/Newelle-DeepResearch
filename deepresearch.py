"""Deep Research extension for Newelle - autonomous multi-step web research.

Re-imagines the LangGraph-based deep research workflow using only Newelle's
native primitives (self.llm, self.websearch, run_llm_with_tools).
"""

import datetime
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from gettext import gettext as _
from urllib.parse import urlparse

import requests
from gi.repository import Gtk, Adw, GLib, Pango, GObject

from .extensions import NewelleExtension
from .tools import Command, Tool, ToolResult
from .ui import load_image_with_callback
from .utility.strings import extract_json


# ------------------------------------------------------------------ #
#  Prompts (adapted from the original deepresearch/prompts.py)
# ------------------------------------------------------------------ #

BREAK_QUERY_PROMPT = """You are a research planner. Given a user's research query, break it down into {num_subqueries} specific, independent sub-queries that can be researched separately.

Each sub-query should:
- Be a self-contained web-search query (like what you would type into Google)
- Cover a different aspect of the original question
- Be specific and detailed enough to produce useful results
- Avoid overlap with other sub-queries

Original query: {query}

Respond in valid JSON format with exactly these keys:
{{
  "sub_queries": ["sub-query 1", "sub-query 2", ...]
}}"""

SYNTHESIZE_SUBQUERY_PROMPT = """You are a research analyst synthesizing findings from web searches.

Original research query: {query}
Sub-query being researched: {sub_query}

Below are the search results gathered for this sub-query:

{search_results}

Please synthesize these findings into a well-organized summary. Follow these guidelines:
1. Preserve all relevant facts, data points, quotes, and sources
2. Include inline citations in [Source N: Title](URL) format
3. Remove redundant or irrelevant information
4. Structure the summary logically
5. Return ONLY the synthesized summary text, no preamble or meta-commentary"""

FINAL_REPORT_PROMPT = """You are a research report writer. Based on the research findings below, write a comprehensive, well-structured final report.

Original research question: {query}

Research findings from multiple sub-queries:

{all_findings}

Today's date is {date}.

Create a detailed report that:
1. Has a clear title (# heading)
2. Is well-organized with proper headings (## for sections, ### for subsections)
3. Includes specific facts and insights from the research
4. References relevant sources using [Title](URL) format
5. Provides a balanced, thorough analysis
6. Ends with a ## Sources section listing all referenced links

Use simple, clear language. Write in a professional, objective tone.
Do not refer to yourself as the writer. Just write the report.

Format the report in clear markdown."""


# ------------------------------------------------------------------ #
#  DeepResearchWidget
# ------------------------------------------------------------------ #

class DeepResearchWidget(Gtk.Box):
    """Widget showing deep research progress and results.

    Styled like the native WebSearchWidget with:
    - A card-like header with a pulsing status label
    - An animated (Gtk.Revealer) list of phase rows
    - A progress bar for overall completion
    - Native Adwaita expanders for per-phase content and the final report
    """

    __gsignals__ = {
        "website-clicked": (GObject.SignalFlags.RUN_LAST, None, (str,)),
    }

    def __init__(self, query: str, **kwargs):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10, **kwargs)
        self.add_css_class("osd")
        self.add_css_class("toolbar")
        self.add_css_class("code")
        self.set_margin_top(10)
        self.set_margin_bottom(10)
        self.set_margin_start(12)
        self.set_margin_end(12)

        self._query = query
        self._phase_rows: list[Adw.ExpanderRow] = []
        self._phase_spinners: list[Gtk.Spinner] = []
        self._phase_revealers: list[Gtk.Revealer] = []
        self._report_label: Gtk.Label | None = None
        self._report_expander: Adw.ExpanderRow | None = None
        self._status_label: Gtk.Label | None = None
        self._spinner: Gtk.Spinner | None = None
        self._header_right: Gtk.Box | None = None
        self._progress_bar: Gtk.ProgressBar | None = None
        self._progress_label: Gtk.Label | None = None

        self._build_ui()

    def _build_ui(self):
        # ---- Header ----
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        header.set_margin_top(8)
        header.set_margin_bottom(8)
        header.set_margin_start(12)
        header.set_margin_end(12)

        icon = Gtk.Image.new_from_icon_name("system-search-symbolic")
        icon.set_pixel_size(24)
        icon.add_css_class("accent")
        header.append(icon)

        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title_box.set_hexpand(True)
        title_label = Gtk.Label(label=_("Deep Research"), xalign=0)
        title_label.add_css_class("heading")
        title_box.append(title_label)

        query_label = Gtk.Label(
            label=self._query if len(self._query) < 120 else self._query[:117] + "\u2026",
            xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
        )
        query_label.add_css_class("dim-label")
        query_label.add_css_class("caption")
        title_box.append(query_label)
        header.append(title_box)

        # Right side: spinner while running
        self._header_right = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._spinner = Gtk.Spinner(spinning=True, visible=True)
        self._header_right.append(self._spinner)
        header.append(self._header_right)

        self.append(header)

        # ---- Progress bar ----
        progress_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        progress_box.set_margin_start(12)
        progress_box.set_margin_end(12)
        progress_box.set_margin_top(4)
        progress_box.set_margin_bottom(4)
        self._progress_bar = Gtk.ProgressBar(show_text=False, fraction=0.0)
        self._progress_bar.set_hexpand(True)
        self._progress_bar.set_valign(Gtk.Align.CENTER)
        self._progress_label = Gtk.Label(label="0 %", css_classes=["dim-label", "caption"])
        self._progress_label.set_valign(Gtk.Align.CENTER)
        self._progress_label.set_size_request(36, -1)
        progress_box.append(self._progress_bar)
        progress_box.append(self._progress_label)
        self.append(progress_box)

        # ---- Phase list (plain box with revealer animations, like WebSearchWidget) ----
        self._phase_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        self._phase_list.set_margin_start(12)
        self._phase_list.set_margin_end(12)
        self.append(self._phase_list)

        # ---- Status label ----
        self._status_label = Gtk.Label(
            label=_("Planning research\u2026"),
            halign=Gtk.Align.START,
            wrap=True,
        )
        self._status_label.add_css_class("pulsing-label")
        self._status_label.add_css_class("dim-label")
        self._status_label.set_margin_start(12)
        self._status_label.set_margin_end(12)
        self.append(self._status_label)

        # ---- Final report (hidden initially) ----
        self._report_label = Gtk.Label(
            wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
            justify=Gtk.Justification.LEFT, selectable=True, xalign=0,
            margin_top=8, margin_bottom=8, margin_start=8, margin_end=8,
        )
        report_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        report_box.append(self._report_label)

        self._report_expander = Adw.ExpanderRow(
            title=_("Research Report"),
            subtitle=_("Click to expand the final report"),
        )
        report_icon = Gtk.Image.new_from_icon_name("text-x-generic-symbolic")
        report_icon.set_pixel_size(16)
        report_icon.add_css_class("accent")
        self._report_expander.add_prefix(report_icon)
        self._report_expander.add_row(report_box)
        self._report_expander.set_visible(False)
        self._report_expander.set_margin_start(12)
        self._report_expander.set_margin_end(12)
        self._report_expander.add_css_class("card")
        self.append(self._report_expander)

    # -- Public API --

    def set_status(self, status: str):
        """Update the status label (thread-safe via GLib.idle_add)."""
        if self._status_label:
            self._status_label.set_text(status)

    def set_progress(self, fraction: float):
        """Update the progress bar and percentage label (0.0 - 1.0)."""
        fraction = min(max(fraction, 0.0), 1.0)
        if self._progress_bar:
            self._progress_bar.set_fraction(fraction)
        if self._progress_label:
            self._progress_label.set_text(f"{int(fraction * 100)} %")

    def add_phase(self, phase_title: str, status: str = "Searching\u2026") -> int:
        """Add a new collapsible phase row for a sub-query.

        The row is wrapped in a Gtk.Revealer so it animates in smoothly.
        Returns the phase index (0-based).
        """
        row = Adw.ExpanderRow(
            title=phase_title if len(phase_title) < 80 else phase_title[:77] + "\u2026",
            subtitle=status,
        )
        # Search icon as prefix (hidden until spinner disappears)
        search_icon = Gtk.Image.new_from_icon_name("system-search-symbolic")
        search_icon.set_pixel_size(16)
        search_icon.add_css_class("accent")
        search_icon.set_visible(False)
        row.add_prefix(search_icon)
        setattr(row, "_dr_search_icon", search_icon)

        # Spinner icon as prefix
        phase_spinner = Gtk.Spinner(spinning=True, visible=True)
        phase_spinner.set_size_request(16, 16)
        row.add_prefix(phase_spinner)
        self._phase_spinners.append(phase_spinner)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        content.set_margin_start(12)
        content.set_margin_end(12)
        content.set_margin_top(4)
        content.set_margin_bottom(4)

        scrolled = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            min_content_height=100,
            max_content_height=350,
            child=content,
        )
        row.add_row(scrolled)

        # Wrap in a revealer for smooth slide-in animation
        revealer = Gtk.Revealer(
            transition_type=Gtk.RevealerTransitionType.SLIDE_UP,
            transition_duration=300,
            reveal_child=False,
        )
        revealer.set_child(row)
        self._phase_list.append(revealer)
        self._phase_rows.append(row)
        self._phase_revealers.append(revealer)

        # Animate in on the next idle tick
        def _reveal(r=revealer):
            r.set_reveal_child(True)
            return False
        GLib.idle_add(_reveal)

        # Attach content references for later updates
        setattr(row, "_dr_content", content)
        setattr(row, "_dr_scrolled", scrolled)
        setattr(row, "_dr_links", [])

        return len(self._phase_rows) - 1

    def add_phase_source(self, phase_idx: int, title: str, url: str):
        """Add a clickable source link to a phase with a favicon."""
        if phase_idx < 0 or phase_idx >= len(self._phase_rows):
            return
        row = self._phase_rows[phase_idx]
        content = getattr(row, "_dr_content")

        source_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        source_box.set_margin_top(2)
        source_box.set_margin_bottom(2)

        # Favicon: show a globe icon initially, then load the real favicon
        favicon = Gtk.Image.new_from_icon_name("internet-symbolic")
        favicon.set_pixel_size(16)
        source_box.append(favicon)

        favicon_url = self._get_favicon_url(url)
        if favicon_url:
            load_image_with_callback(
                favicon_url,
                lambda loader, img=favicon: img.set_from_pixbuf(loader.get_pixbuf()),
            )

        label = Gtk.Label(
            label=title if len(title) < 60 else title[:57] + "\u2026",
            xalign=0, hexpand=True,
            ellipsize=Pango.EllipsizeMode.END,
            tooltip_text=url,
        )
        source_box.append(label)

        button = Gtk.Button(child=source_box)
        button.set_has_frame(False)
        button.add_css_class("flat")
        button.connect("clicked", lambda btn, u=url: self.emit("website-clicked", u))

        content.append(button)

        links = getattr(row, "_dr_links")
        links.append((title, url))

    @staticmethod
    def _get_favicon_url(url: str) -> str:
        """Derive a favicon URL from a page URL using Google's favicon service."""
        try:
            parsed = urlparse(url)
            domain = parsed.netloc
            return f"https://www.google.com/s2/favicons?domain={domain}&sz=32"
        except Exception:
            return ""

    def set_phase_summary(self, phase_idx: int, summary: str):
        """Set the synthesized summary text for a phase."""
        if phase_idx < 0 or phase_idx >= len(self._phase_rows):
            return
        row = self._phase_rows[phase_idx]
        content = getattr(row, "_dr_content")

        # Remove any previous summary label
        for child in list(content):
            if hasattr(child, "_dr_summary"):
                content.remove(child)

        summary_label = Gtk.Label(
            label=summary, xalign=0,
            wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, selectable=True,
        )
        summary_label.set_margin_top(6)
        summary_label._dr_summary = True
        content.append(summary_label)

    def finish_phase(self, phase_idx: int, status: str = "Done", success: bool = True):
        """Mark a phase as complete, stopping the spinner and adding a checkmark/error suffix."""
        if phase_idx < 0 or phase_idx >= len(self._phase_rows):
            return
        row = self._phase_rows[phase_idx]
        row.set_subtitle(status)

        # Stop and hide the phase spinner, show the search icon
        if phase_idx < len(self._phase_spinners):
            spinner = self._phase_spinners[phase_idx]
            spinner.stop()
            spinner.set_visible(False)
        search_icon = getattr(row, "_dr_search_icon", None)
        if search_icon is not None:
            search_icon.set_visible(True)

        # Add a completion icon as suffix (if not already there)
        if not getattr(row, "_dr_finished", False):
            icon_name = "object-select-symbolic" if success else "dialog-error-symbolic"
            finished_icon = Gtk.Image.new_from_icon_name(icon_name)
            finished_icon.set_pixel_size(16)
            if success:
                finished_icon.add_css_class("success")
            else:
                finished_icon.add_css_class("error")
            row.add_suffix(finished_icon)
            setattr(row, "_dr_finished", True)

    def finish(self, report: str):
        """Show the final report, hide the header spinner, and dim the progress bar."""
        if self._spinner and self._header_right:
            self._spinner.stop()
            self._header_right.remove(self._spinner)
            self._spinner = None
        if self._status_label:
            self._status_label.set_text(_("Research complete"))
            self._status_label.remove_css_class("pulsing-label")
            self._status_label.remove_css_class("dim-label")
        if self._progress_bar:
            self._progress_bar.set_fraction(1.0)
            self._progress_bar.add_css_class("dim-label")
        if self._progress_label:
            self._progress_label.set_text("100 %")
        if self._report_label:
            self._report_label.set_text(report)
        if self._report_expander:
            self._report_expander.set_visible(True)



# ------------------------------------------------------------------ #
#  DeepResearchIntegration
# ------------------------------------------------------------------ #

class DeepResearchIntegration(NewelleExtension):
    """Deep Research tool for autonomous multi-step web research.

    Provides the ``deepresearch`` tool that:
    1. Breaks a query into sub-queries using the LLM
    2. Searches each sub-query via the configured websearch handler
    3. Synthesises per-sub-query findings using the LLM
    4. Generates a comprehensive final report from all findings
    """

    id = "deepresearch"
    name = "Deep Research"

    def __init__(self, pip_path, extension_path, settings):
        super().__init__(pip_path, extension_path, settings)
        self._research_cache: dict[str, dict] = {}  # tool_uuid -> {query, depth, language, report}

    # -------------------------------------------------------------- #
    #  Tool entry point  (runs on main thread → spawns worker)
    # -------------------------------------------------------------- #

    def deepresearch(
        self,
        query: str,
        depth: int = 3,
        language: str = "",
        tool_uuid: str = None,
    ) -> ToolResult:
        """Execute a deep research task.

        Args:
            query: The research question / topic.
            depth: Number of sub-queries (1–10, default 3).
            language: If set, write the final report in this language.
            tool_uuid: Internal tool call identifier.
        """
        depth = max(1, min(depth, 10))

        # Pre-flight: validate prerequisites *before* creating GTK widgets
        if self.websearch is None:
            result = ToolResult()
            result.set_output(_("Deep research is unavailable: web search is not configured."))
            return result
        if self.llm is None:
            result = ToolResult()
            result.set_output(_("Deep research is unavailable: no LLM configured."))
            return result

        # Create result and widget on the main thread
        result = ToolResult()
        widget = DeepResearchWidget(query)
        result.set_widget(widget)
        result.set_display_text(_("Deep researching: ") + query)

        # Connect website clicks to the UI controller's link opener
        widget.connect(
            "website-clicked",
            lambda w, link: self.ui_controller.open_link(
                link, False, not self.settings.get_boolean("external-browser")
            ),
        )

        def run():
            try:
                self._execute_research(query, depth, language, widget, result, tool_uuid)
            except Exception as e:
                report = _("Deep research failed: ") + str(e)
                GLib.idle_add(widget.finish, report)
                result.set_output(report)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return result

    # -------------------------------------------------------------- #
    #  Restore (chat reload)
    # -------------------------------------------------------------- #

    def _restore_deepresearch(
        self,
        tool_uuid: str,
        query: str,
        depth: int = 3,
        language: str = "",
    ) -> ToolResult:
        """Rebuild a completed DeepResearchWidget from cached data on chat reload."""
        result = ToolResult()

        cached = self._research_cache.get(str(tool_uuid), {})
        report = cached.get("report") or self.ui_controller.get_tool_result_by_id(tool_uuid) or ""

        widget = DeepResearchWidget(query)
        widget.connect(
            "website-clicked",
            lambda w, link: self.ui_controller.open_link(
                link, False, not self.settings.get_boolean("external-browser"),
            ),
        )
        if report:
            widget.finish(report)
        else:
            widget.finish(_("Research results not available for this chat session."))

        result.set_widget(widget)
        result.set_output(report or None)
        return result

    # -------------------------------------------------------------- #
    #  Research workflow (runs on worker thread)
    # -------------------------------------------------------------- #

    def _execute_research(
        self,
        query: str,
        depth: int,
        language: str,
        widget: DeepResearchWidget,
        result: ToolResult,
        tool_uuid: str | None,
    ):
        # ---- Phase 1: Break query into sub-queries ----
        GLib.idle_add(widget.set_status, _("Planning research strategy\u2026"))
        GLib.idle_add(widget.set_progress, 0.05)

        sub_queries = self._generate_sub_queries(query, depth)
        if not sub_queries:
            GLib.idle_add(widget.finish, _("Could not generate a research plan."))
            result.set_output(_("Failed to generate sub-queries."))
            return

        total_steps = len(sub_queries) + 1  # +1 for final report
        GLib.idle_add(
            widget.set_status,
            _("Researching {n} sub-topics\u2026").format(n=len(sub_queries)),
        )

        # ---- Phase 2: Research each sub-query ----
        all_findings: list[str] = []

        for i, sq in enumerate(sub_queries):
            progress = (i + 1) / total_steps
            GLib.idle_add(widget.set_progress, progress)
            GLib.idle_add(
                widget.set_status,
                _("Researching topic {i}/{n}\u2026").format(i=i + 1, n=len(sub_queries)),
            )

            # Synchronised phase creation: the worker thread waits for the
            # main thread to create the row before using its index.
            phase_idx_container: list[int] = []
            created = threading.Event()

            def _add_phase():
                idx = widget.add_phase(sq)
                phase_idx_container.append(idx)
                created.set()

            GLib.idle_add(_add_phase)

            if not created.wait(timeout=5.0):
                # Main thread unresponsive – skip this phase
                continue

            phase_idx = phase_idx_container[0]

            # --- Search ---
            GLib.idle_add(
                widget.set_status,
                _("Searching for: ") + (sq[:50] + "\u2026" if len(sq) > 50 else sq),
            )

            search_text, search_sources = self._web_search(sq)

            # Register sources in widget
            for title, url in search_sources[:8]:
                GLib.idle_add(widget.add_phase_source, phase_idx, title, url)

            GLib.idle_add(widget.finish_phase, phase_idx, _("Synthesizing\u2026"))

            # --- Synthesize findings ---
            synthesized = self._synthesize_findings(query, sq, search_text)
            all_findings.append(f"## Sub-topic: {sq}\n\n{synthesized}")

            preview = synthesized[:500] + "\u2026" if len(synthesized) > 500 else synthesized
            GLib.idle_add(widget.set_phase_summary, phase_idx, preview)
            GLib.idle_add(widget.finish_phase, phase_idx, _("Done"))

        # ---- Phase 3: Generate final report ----
        GLib.idle_add(widget.set_status, _("Writing final report\u2026"))

        report = self._generate_final_report(query, all_findings, language)

        # Cache the report for restore
        if tool_uuid:
            self._research_cache[str(tool_uuid)] = {
                "query": query,
                "depth": depth,
                "language": language,
                "report": report,
            }

        GLib.idle_add(widget.finish, report)
        result.set_output(report)

    # -- Internal helpers --

    def _generate_sub_queries(self, query: str, depth: int) -> list[str]:
        """Use the LLM to break the main query into sub-queries."""
        prompt = BREAK_QUERY_PROMPT.format(query=query, num_subqueries=depth)
        try:
            response = self.llm.send_message(
                message=prompt,
                history=[],
                system_prompt=[_("You are a helpful research planner. Respond only with valid JSON.")],
            )
            json_str = extract_json(response)
            data = json.loads(json_str)
            return data.get("sub_queries", [])
        except Exception:
            # Fallback: return the original query as the sole research item
            return [query]

    def _web_search(self, query: str) -> tuple[str, list[tuple[str, str]]]:
        """Execute a web search and return (text, [(title, url), ...])."""
        try:
            text, urls = self.websearch.query(query, max_results=5)
            sources = []
            with ThreadPoolExecutor(max_workers=min(len(urls), 5)) as executor:
                futures = {
                    executor.submit(self._fetch_page_title, url): url
                    for url in urls
                }
                results: dict[str, str] = {}
                try:
                    for future in as_completed(futures, timeout=8.0):
                        url = futures[future]
                        try:
                            results[url] = future.result(timeout=1.0)
                        except Exception:
                            results[url] = self._fallback_title(url)
                except TimeoutError:
                    pass  # Use partial results; remaining URLs get fallback titles
                # Preserve original order, fill gaps with fallback titles
                for url in urls:
                    title = results.get(url) or self._fallback_title(url)
                    sources.append((title, url))
            return text, sources
        except Exception as e:
            return _("Search failed: ") + str(e), []

    @staticmethod
    def _fetch_page_title(url: str, timeout: float = 4.0) -> str:
        """Fetch the <title> from a web page. Falls back to domain name."""
        try:
            resp = requests.get(url, timeout=timeout, headers={
                "User-Agent": "Mozilla/5.0 (compatible; NewelleDeepResearch/1.0)",
            })
            if resp.status_code == 200:
                match = re.search(r"<title[^>]*>(.*?)</title>", resp.text, re.IGNORECASE | re.DOTALL)
                if match:
                    title = re.sub(r"\s+", " ", match.group(1)).strip()
                    if title:
                        return title[:120]
        except Exception:
            pass
        return DeepResearchIntegration._fallback_title(url)

    @staticmethod
    def _fallback_title(url: str) -> str:
        """Return a human-readable title from a URL (domain name)."""
        try:
            return urlparse(url).netloc
        except Exception:
            return url

    def _synthesize_findings(
        self, original_query: str, sub_query: str, search_results: str
    ) -> str:
        """Synthesize search results for a single sub-query."""
        max_len = 12000
        if len(search_results) > max_len:
            search_results = search_results[:max_len] + "\n\u2026[truncated]"

        prompt = SYNTHESIZE_SUBQUERY_PROMPT.format(
            query=original_query,
            sub_query=sub_query,
            search_results=search_results,
        )
        try:
            return self.llm.send_message(
                message=prompt,
                history=[],
                system_prompt=[_("You are a helpful research analyst. Synthesize search findings accurately.")],
            )
        except Exception as e:
            return _("Synthesis failed: ") + str(e)

    def _generate_final_report(
        self, query: str, all_findings: list[str], language: str
    ) -> str:
        """Generate the final comprehensive report."""
        date_str = datetime.datetime.now().strftime("%a %b %d, %Y")
        findings_text = "\n\n---\n\n".join(all_findings)

        max_len = 25000
        if len(findings_text) > max_len:
            findings_text = findings_text[:max_len] + "\n\n\u2026[truncated]"

        prompt = FINAL_REPORT_PROMPT.format(
            query=query,
            all_findings=findings_text,
            date=date_str,
        )

        extra_instruction = ""
        if language:
            extra_instruction = f" Write the entire report in {language}."

        try:
            return self.llm.send_message(
                message=prompt,
                history=[],
                system_prompt=[
                    _("You are a professional research report writer.") + extra_instruction,
                ],
            )
        except Exception as e:
            return "# Research Report\n\n" + findings_text + f"\n\n*Report generation failed: {e}*"

    # -------------------------------------------------------------- #
    #  Tool & command registration
    # -------------------------------------------------------------- #

    def get_tools(self) -> list:
        return [
            Tool(
                name="deepresearch",
                description=(
                    "Perform autonomous multi-step deep research on a topic. "
                    "This tool will break the query into sub-questions, search the web "
                    "for each one, synthesize findings, and generate a comprehensive report. "
                    "Use this for complex research questions that require thorough investigation. "
                    "Arguments: query (required) - the research question; "
                    "depth (optional, default 3) - number of sub-queries (1-10); "
                    "language (optional) - language for the final report."
                ),
                func=self.deepresearch,
                restore_func=self._restore_deepresearch,
                title="Deep Research",
                default_on=True,
                icon_name="system-search-symbolic",
                tools_group=_("Agent"),
                schema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The research question or topic to investigate.",
                        },
                        "depth": {
                            "type": "integer",
                            "description": "Number of sub-queries to use (1-10, default 3). Higher = more thorough.",
                        },
                        "language": {
                            "type": "string",
                            "description": "Language for the final report (e.g., 'Italian', 'English'). Leave empty to auto-detect.",
                        },
                    },
                    "required": ["query"],
                },
            ),
        ]

    def get_commands(self) -> list:
        return [
            Command(
                "deepresearch",
                "Perform deep research on a topic.",
                self.deepresearch,
                icon_name="system-search-symbolic",
            ),
        ]

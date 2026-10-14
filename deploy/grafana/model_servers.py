"""Generates the "all metrics" dashboards of model servers: python3 deploy/grafana/model_servers.py

gman-llm-all.json (vLLM, vllm:*) and gman-triton.json (Triton, nv_*). The panels are not a list of names: Grafana
repeats a panel for every gauge, counter and histogram of the server found in Prometheus, so metrics of new server
versions appear without changes here. Triton also gets a summary row computed from its cumulative counters.
"""
import json
from pathlib import Path

DS = {"type": "prometheus", "uid": "prometheus"}
# Scrape bookkeeping added by the Collector and the gateway: dropped so legends show only server and GMan labels.
NOISE = ("job, instance, exported_job, exported_instance, otel_scope_name, otel_scope_version, server_address, "
         "server_port, service_instance_id, service_name, url_scheme")


class Board:
    def __init__(self):
        self.panels, self.y, self.next_id = [], 0, 1

    def add(self, panel, h):
        panel["id"], self.next_id = self.next_id, self.next_id + 1
        panel["gridPos"]["y"] = self.y
        self.panels.append(panel)
        return panel

    def row(self, title):
        self.add({"type": "row", "title": title, "collapsed": False, "panels": [],
                  "gridPos": {"x": 0, "w": 24, "h": 1}}, 1)
        self.y += 1

    def line(self, *panels, h=8):
        """Panels side by side; each is (builder, width)."""
        x = 0
        for panel, w in panels:
            panel["gridPos"] = {"x": x, "w": w, "h": h}
            self.add(panel, h)
            x += w
        self.y += h


def target(expr, legend="__auto", ref="A", table=False):
    t = {"datasource": DS, "refId": ref, "expr": expr, "legendFormat": legend}
    if table:
        t.update(instant=True, range=False, format="table")
    return t


def series(title, description, targets, unit="", repeat=None, maxPerRow=3):
    p = {"type": "timeseries", "title": title, "description": description, "datasource": DS,
         "fieldConfig": {"defaults": {"custom": {"fillOpacity": 5, "stacking": {"mode": "none"}, "showPoints": "never",
                                                 "lineWidth": 1, "spanNulls": False}}, "overrides": []},
         "options": {"legend": {"displayMode": "list", "placement": "bottom", "calcs": []},
                     "tooltip": {"mode": "multi", "sort": "desc"}},
         "targets": targets}
    if unit:
        p["fieldConfig"]["defaults"]["unit"] = unit
    if repeat:
        p.update(repeat=repeat, repeatDirection="h", maxPerRow=maxPerRow)
    return p


def table(title, description, expr, rename=None, mappings=None, exclude=("Time",)):
    p = {"type": "table", "title": title, "description": description, "datasource": DS,
         "fieldConfig": {"defaults": {"noValue": "—"}, "overrides": []},
         "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}},
         "targets": [target(expr, "", table=True)],
         "transformations": [{"id": "organize", "options": {
             "excludeByName": {name: True for name in exclude}, "renameByName": rename or {}}}]}
    if mappings:
        p["fieldConfig"]["overrides"] = [{"matcher": {"id": "byName", "options": list(rename.values())[0]},
                                          "properties": [{"id": "mappings", "value": mappings},
                                                         {"id": "custom.cellOptions", "value": {"type": "color-text"}}]}]
    return p


def variable(name, label, query, regex="", all_value=None):
    v = {"name": name, "label": label, "type": "query", "datasource": DS,
         "query": {"query": query, "refId": name}, "definition": query, "refresh": 2, "sort": 1,
         "includeAll": True, "multi": True, "current": {"text": "All", "value": "$__all"}}
    if regex:
        v["regex"] = regex
    if all_value:
        v["allValue"] = all_value
    return v


def dashboard(uid, title, description, job, prefix, model_label, model_title, extra_rows=None, info=False):
    """`prefix` matches the server's metric names (a regex), `model_label` is its model label."""
    sel = f'gman_service=~"$service", {model_label}=~"$model"'
    any_metric = f'{{__name__=~"{prefix}.+"}}'
    board = Board()
    board.row("Серверы и модели")
    board.line(
        (table("Серверы", f"Все серверы {title}, которые опрашивают Collector-ы, и отвечает ли их /metrics.",
               'max by (gman_service, gman_environment, host_id, container_name, namespace, pod, exported_instance) '
               f'(up{{job="otel", exported_job="{job}", gman_service=~"$service"}})',
               rename={"Value": "Состояние"},
               mappings=[{"type": "value", "options": {"1": {"text": "отвечает", "color": "green"},
                                                       "0": {"text": "не отвечает", "color": "red"}}}]), 12),
        (table("Модели", f"{model_title} по сервисам и число реплик, которые сейчас отдают метрики.",
               f'count by (gman_service, gman_environment, {model_label}{", version" if job == "triton" else ""}) '
               f'(group by (gman_service, gman_environment, {model_label}{", version" if job == "triton" else ""}, '
               f'host_id, container_name, pod) ({{__name__=~"{prefix}.+", {model_label}!="", {sel}}}))',
               rename={"Value": "Реплик"}), 12))
    if info:
        board.line((table("Конфигурация серверов (info-метрики)",
                          f"Все метрики {prefix}*_info как есть: параметры, которые сервер публикует метками.",
                          f'{{__name__=~"{prefix}.+_info", {sel}}}', exclude=("Time", "Value")), 24))
    for title_, rows in (extra_rows or []):
        board.row(title_)
        for row in rows:
            board.line(*row)
    board.row("Текущие значения (gauges)")
    board.line((series("$gauge", "Значение как экспортирует сервер, каждая серия отдельно.",
                       [target(f"max without ({NOISE}) ($gauge{{{sel}}})")], repeat="gauge"), 8))
    board.row("Счетчики: скорость в секунду")
    board.line((series("$counter", "rate() за интервал графика, каждая серия отдельно.",
                       [target(f"sum without ({NOISE}) (rate($counter{{{sel}}}[$__rate_interval]))")],
                       repeat="counter"), 8))
    board.row("Гистограммы: перцентили и среднее")
    by = f"gman_service, {model_label}"
    hist = [target(f"histogram_quantile({q}, sum by ({by}, le) (rate(${{histogram}}_bucket{{{sel}}}[$__rate_interval])))",
                   f"p{int(q * 100)} {{{{gman_service}}}} {{{{{model_label}}}}}", ref)
            for q, ref in ((0.5, "A"), (0.95, "B"), (0.99, "C"))]
    hist.append(target(f"sum by ({by}) (rate(${{histogram}}_sum{{{sel}}}[$__rate_interval])) / "
                       f"sum by ({by}) (rate(${{histogram}}_count{{{sel}}}[$__rate_interval]))",
                       f"среднее {{{{gman_service}}}} {{{{{model_label}}}}}", "D"))
    board.line((series("$histogram", "Перцентили по бакетам и среднее (sum/count) за интервал графика по сервису и модели.",
                       hist, repeat="histogram", maxPerRow=2), 12))
    return {
        "uid": uid, "title": f"GMan · {title} · все метрики" if job == "vllm" else f"GMan · {title}",
        "description": description, "tags": ["gman"], "timezone": "browser", "schemaVersion": 39, "version": 1,
        "editable": True, "refresh": "30s", "time": {"from": "now-1h", "to": "now"},
        "templating": {"list": [
            variable("service", "Сервис", f"label_values({any_metric}, gman_service)", all_value=".*"),
            variable("model", "Модель", f'label_values({{__name__=~"{prefix}.+", gman_service=~"$service"}}, {model_label})',
                     all_value=".*"),
            # Summaries arrive as the base name (with quantile) plus _sum/_count: the base name is shown as a gauge.
            variable("gauge", "Gauges", f"metrics({prefix}.+)",
                     rf"/^(?!.*_(?:total|bucket|sum|count|created|info)$)({prefix}.+)$/"),
            variable("counter", "Счетчики", f"metrics({prefix}.+_total)"),
            variable("histogram", "Гистограммы", f"metrics({prefix}.+_bucket)", rf"/^({prefix}.+)_bucket$/"),
        ]},
        "links": [{"title": "Дашборды GMan", "type": "dashboards", "tags": ["gman"], "asDropdown": False,
                   "keepTime": True, "includeVars": False, "targetBlank": False, "icon": "external link"}],
        "panels": board.panels,
    }


def triton_summary():
    """Triton reports cumulative counters (durations in microseconds): rates per model give requests/s and
    the average time of a request and of its phases."""
    sel = 'gman_service=~"$service", model=~"$model"'
    by = "gman_service, model"

    def per_request(metric):
        return (f"sum by ({by}) (rate({metric}{{{sel}}}[$__rate_interval])) / 1000 / "
                f"sum by ({by}) (rate(nv_inference_request_success_total{{{sel}}}[$__rate_interval]))")

    phases = [("nv_inference_request_duration_us_total", "весь запрос", "A"),
              ("nv_inference_queue_duration_us_total", "очередь", "B"),
              ("nv_inference_compute_input_duration_us_total", "вход", "C"),
              ("nv_inference_compute_infer_duration_us_total", "инференс", "D"),
              ("nv_inference_compute_output_duration_us_total", "выход", "E")]
    return [[
        (series("Запросы/с", "Успешные и неуспешные запросы инференса в секунду по моделям.", [
            target(f"sum by ({by}) (rate(nv_inference_request_success_total{{{sel}}}[$__rate_interval]))",
                   "успешно {{gman_service}} {{model}}"),
            target(f"sum by ({by}, reason) (rate(nv_inference_request_failure_total{{{sel}}}[$__rate_interval]))",
                   "ошибка {{reason}} {{gman_service}} {{model}}", "B")], "reqps"), 8),
        (series("Среднее время запроса по фазам", "Накопленная длительность (мкс) / успешные запросы за интервал: "
                "весь запрос, ожидание в очереди, подготовка входа, инференс, выдача результата.",
                [target(per_request(m), f"{name} {{{{gman_service}}}} {{{{model}}}}", ref) for m, name, ref in phases],
                "ms"), 8),
        (series("Запросов в очереди", "nv_inference_pending_request_count: запросы, принятые сервером и еще не "
                "отданные на исполнение.", [target(f"sum by ({by}) (nv_inference_pending_request_count{{{sel}}})",
                                                  "{{gman_service}} {{model}}")]), 8),
    ], [
        (series("Батчинг: инференсов на исполнение", "nv_inference_count / nv_inference_exec_count: сколько "
                "запросов в среднем объединяется в один запуск модели (1 — батчинг не работает).",
                [target(f"sum by ({by}) (rate(nv_inference_count_total{{{sel}}}[$__rate_interval])) / "
                        f"sum by ({by}) (rate(nv_inference_exec_count_total{{{sel}}}[$__rate_interval]))",
                        "{{gman_service}} {{model}}")]), 8),
        (series("Загрузка GPU (по данным Triton)", "nv_gpu_utilization по GPU сервера.",
                [target('max by (gman_service, host_id, gpu_uuid) (nv_gpu_utilization{gman_service=~"$service"})',
                        "{{gman_service}} {{host_id}} {{gpu_uuid}}")], "percentunit"), 8),
        (series("VRAM (по данным Triton)", "nv_gpu_memory_used_bytes и общий объем по GPU сервера.", [
            target('max by (gman_service, host_id, gpu_uuid) (nv_gpu_memory_used_bytes{gman_service=~"$service"})',
                   "занято {{gman_service}} {{host_id}} {{gpu_uuid}}"),
            target('max by (gman_service, host_id, gpu_uuid) (nv_gpu_memory_total_bytes{gman_service=~"$service"})',
                   "всего {{gman_service}} {{host_id}} {{gpu_uuid}}", "B")], "bytes"), 8),
    ]]


BOARDS = {
    "gman-llm-all.json": dashboard(
        "gman-llm-all", "vLLM",
        "Все метрики vllm:*, которые есть в Prometheus, без списка имен: панели строятся по найденным метрикам, "
        "поэтому новые метрики новых версий vLLM появляются сами. Сводка — «GMan · LLM».",
        "vllm", "vllm:", "model_name", "Модели (--served-model-name)", info=True),
    "gman-triton.json": dashboard(
        "gman-triton", "Triton",
        "Серверы NVIDIA Triton Inference Server: сводка по запросам, задержкам, очереди, батчингу и GPU, затем все "
        "метрики nv_*, которые есть в Prometheus, без списка имен.",
        "triton", "nv_", "model", "Модели (и версии)", extra_rows=[("Сводка", triton_summary())]),
}

if __name__ == "__main__":
    out = Path(__file__).resolve().parent / "dashboards"
    for name, board in BOARDS.items():
        (out / name).write_text(json.dumps(board, ensure_ascii=False, indent=1) + "\n")
        print(out / name)

"""Catalog of named diagnostic services (docs/ARCHITECTURE.md §4.4).

Sequences can use `service: Fingerprint_Write` instead of raw bytes. Sources:
ODX-D (inside a PDX or standalone, through odxtools) and a YAML catalog for
services written by hand. CANdela `.cdd` is not supported yet: the format is not
public and the importer needs real files to be validated against (open question Q-08).
"""

from __future__ import annotations

import difflib
import os
from dataclasses import dataclass, field
from typing import Any, Callable

import yaml


class CatalogError(Exception):
    pass


@dataclass
class ServiceDef:
    name: str
    request: str | None = None  # hex template with ${param} placeholders (YAML catalog)
    encoder: Callable[..., bytes] | None = None  # ODX: odxtools DiagService.encode_request
    params: list[str] = field(default_factory=list)
    description: str = ""
    source: str = ""


class DiagCatalog:
    def __init__(self) -> None:
        self.services: dict[str, ServiceDef] = {}
        self.sources: list[str] = []
        self.keep_alive: list = []  # objects the encoders depend on (ODX databases)

    def add(self, service: ServiceDef) -> None:
        self.services[service.name] = service

    def merge(self, other: DiagCatalog) -> DiagCatalog:
        self.services.update(other.services)
        self.sources += other.sources
        self.keep_alive += other.keep_alive
        return self

    def get(self, name: str) -> ServiceDef:
        try:
            return self.services[name]
        except KeyError:
            close = difflib.get_close_matches(name, self.services, n=3)
            hint = f" (did you mean {', '.join(close)}?)" if close else ""
            raise CatalogError(f"service '{name}' is not in the diagnostic catalog{hint}") from None

    def __contains__(self, name: str) -> bool:
        return name in self.services

    def __len__(self) -> int:
        return len(self.services)


def load_yaml_catalog(path: str) -> DiagCatalog:
    """services: {Name: {request: "2E F1 5A ${data}", params: [data], description: "..."}}"""
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise CatalogError(f"cannot read catalog {path}: {exc}") from exc
    catalog = DiagCatalog()
    for name, spec in (data.get("services") or {}).items():
        if isinstance(spec, str):
            spec = {"request": spec}
        if not isinstance(spec, dict) or "request" not in spec:
            raise CatalogError(f"{path}: service '{name}' needs 'request'")
        catalog.add(ServiceDef(name=str(name), request=str(spec["request"]), params=list(spec.get("params") or []),
                               description=str(spec.get("description", "")), source=path))
    catalog.sources.append(path)
    return catalog


def load_odx_catalog(path: str, layer: str | None = None) -> DiagCatalog:
    """Services of one diagnostic layer (ECU variant or base variant) of a PDX / ODX-D file."""
    import odxtools

    try:
        db = odxtools.load_file(path, use_weakrefs=False)  # services must outlive this function
    except Exception as exc:
        raise CatalogError(f"cannot load ODX database {path}: {exc}") from exc
    layers = {d.short_name: d for d in db.diag_layers}
    if layer is None:
        preferred = [d.short_name for d in db.ecu_variants] or [d.short_name for d in db.base_variants]
        if len(preferred) != 1:
            raise CatalogError(f"{os.path.basename(path)} has several diagnostic layers, choose one with "
                               f"'layer': {', '.join(sorted(layers))}")
        layer = preferred[0]
    if layer not in layers:
        raise CatalogError(f"no diagnostic layer '{layer}' in {path} (available: {', '.join(sorted(layers))})")
    catalog = DiagCatalog()
    for service in layers[layer].services:
        if service.request is None:
            continue

        def encode(_s=service, **params: Any) -> bytes:
            return bytes(_s.encode_request(**params))

        params = [p.short_name for p in service.request.parameters if p.is_settable]
        catalog.add(ServiceDef(name=service.short_name, encoder=encode, params=params,
                               description=service.long_name or "", source=f"{path}#{layer}"))
    catalog.sources.append(f"{path}#{layer}")
    catalog.keep_alive.append(db)
    return catalog


def load_catalog(ref: str | dict, base_dir: str = ".") -> DiagCatalog:
    """`ref`: a path, or {file: path, layer: ShortName} for ODX databases with several layers."""
    if isinstance(ref, dict):
        path, layer = str(ref.get("file", "")), ref.get("layer")
    else:
        path, layer = str(ref), None
    if not path:
        raise CatalogError("catalog entry needs a file")
    if not os.path.isabs(path):
        path = os.path.join(base_dir, path)
    ext = os.path.splitext(path)[1].lower()
    if ext in (".yaml", ".yml"):
        return load_yaml_catalog(path)
    if ext in (".pdx", ".odx-d", ".odx"):
        return load_odx_catalog(path, layer)
    if ext == ".cdd":
        raise CatalogError("CANdela .cdd import is not available yet: it needs sample files to be validated "
                           "(open question Q-08). Export the database as ODX-D/PDX or use a YAML catalog.")
    raise CatalogError(f"unsupported catalog type '{ext}' ({path})")

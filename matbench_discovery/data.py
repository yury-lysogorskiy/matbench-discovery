"""Download, cache and hydrate data files from the Matbench Discovery Figshare article.

https://figshare.com/articles/dataset/22715158

Environment Variables:
    MBD_AUTO_DOWNLOAD_FILES: Controls whether to auto-download missing data files.
        Defaults to "true". Set to "false" to be prompted before downloading.
        This affects both model prediction files and dataset files.
    MBD_CACHE_DIR: Directory to cache downloaded data files.
        Defaults to DATA_DIR if the full repo was cloned, otherwise ~/.cache/matbench-discovery.
"""

import abc
import builtins
import functools
import io
import os
import sys
import traceback
import zipfile
from collections import defaultdict
from collections.abc import Callable, Sequence
from enum import EnumMeta, StrEnum, _EnumDict
from glob import glob
from pathlib import Path
from typing import Any, Literal, Self, TypeVar

import ase.io
import pandas as pd
import plotly.express as px
import requests
import yaml
from ase import Atoms
from pymatviz.enums import Key
from ruamel.yaml import YAML
from tqdm import tqdm

from matbench_discovery import DATA_DIR, PKG_DIR, ROOT, TEST_FILES
from matbench_discovery.enums import MbdKey, TestSubset

T = TypeVar("T", bound="Files")

# repo URL to raw files on GitHub
RAW_REPO_URL = "https://github.com/janosh/matbench-discovery/raw"
# directory to cache downloaded data files
DEFAULT_CACHE_DIR = os.getenv(
    "MBD_CACHE_DIR",
    DATA_DIR  # use DATA_DIR to locally cache data files if full repo was cloned
    if os.path.isdir(DATA_DIR)
    # use ~/.cache if matbench-discovery was installed from PyPI
    else os.path.expanduser("~/.cache/matbench-discovery"),
)

round_trip_yaml = YAML()  # round-trippable YAML for updating model metadata files
round_trip_yaml.preserve_quotes = True
round_trip_yaml.width = 1000  # avoid changing line wrapping
round_trip_yaml.indent(mapping=2, sequence=4, offset=2)


def as_dict_handler(obj: Any) -> dict[str, Any] | None:
    """Pass this to json.dump(default=) or as pandas.to_json(default_handler=) to
    serialize Python classes with as_dict(). Warning: Objects without a as_dict() method
    are replaced with None in the serialized data.
    """
    try:
        return obj.as_dict()  # all MSONable objects implement as_dict()
    except AttributeError:
        return None  # replace unhandled objects with None in serialized data
        # removes e.g. non-serializable AseAtoms from M3GNet relaxation trajectories


def glob_to_df(
    pattern: str,
    *,
    reader: Callable[[Any], pd.DataFrame] | None = None,
    pbar: bool = True,
    **kwargs: Any,
) -> pd.DataFrame:
    """Combine data files matching a glob pattern into a single dataframe.

    Args:
        pattern (str): Glob file pattern.
        reader (Callable[[Any], pd.DataFrame], optional): Function that loads data from
            disk. Defaults to pd.read_csv if ".csv" in pattern else pd.read_json.
        pbar (bool, optional): Whether to show progress bar. Defaults to True.
        **kwargs: Keyword arguments passed to reader (i.e. pd.read_csv or pd.read_json).

    Returns:
        pd.DataFrame: Combined dataframe.

    Raises:
        FileNotFoundError: If no files match the glob pattern.
        ValueError: If reader is None and the file extension is unrecognized.
    """
    if reader is None:
        if ".csv" in pattern.lower():
            reader = pd.read_csv
        elif ".json" in pattern.lower():
            reader = pd.read_json
        else:
            raise ValueError(f"Unsupported file extension in {pattern=}")

    files = glob(pattern)

    if len(files) == 0:
        # load mocked model predictions when running pytest (just first 500 lines
        # from MACE-MPA-0 WBM energy preds)
        if "pytest" in sys.modules or "CI" in os.environ:
            df_mock = pd.read_csv(f"{TEST_FILES}/mock-wbm-energy-preds.csv.gz")
            # .set_index( "material_id" )
            # make sure pred_cols for all models are present in df_mock
            for model in Model:
                with open(model.yaml_path) as file:
                    model_data = yaml.safe_load(file)

                pred_col = (
                    model_data.get("metrics", {}).get("discovery", {}).get("pred_col")
                )
                df_mock[pred_col] = df_mock["e_form_per_atom"]
            return df_mock
        raise FileNotFoundError(f"No files matching glob {pattern=}")

    sub_dfs = {}  # used to join slurm job array results into single df
    for file in tqdm(files, disable=not pbar):
        df_i = reader(file, **kwargs)
        sub_dfs[file] = df_i

    return pd.concat(sub_dfs.values())


def ase_atoms_from_zip(
    zip_filename: str | Path,
    *,
    file_filter: Callable[[str], bool] = lambda fname: fname.endswith(".extxyz"),
    filename_to_info: bool = False,
    limit: int | None = None,
) -> list[Atoms]:
    """Read ASE Atoms objects from a ZIP file containing extXYZ files.

    Args:
        zip_filename (str): Path to the ZIP file.
        file_filter (Callable[[str], bool], optional): Function to check if a file
            should be read. Defaults to lambda fname: fname.endswith(".extxyz").
        filename_to_info (bool, optional): If True, assign filename to Atoms.info.
            Defaults to False.
        limit (int, optional): Maximum number of files to read. Defaults to None.
            Use a small number to speed up debugging runs.

    Returns:
        list[Atoms]: ASE Atoms objects.
    """
    atoms_list = []
    with zipfile.ZipFile(zip_filename) as zip_file:
        desc = f"Reading ASE Atoms from {zip_filename=}"
        for filename in tqdm(zip_file.namelist()[:limit], desc=desc, mininterval=5):
            if not file_filter(filename):
                continue
            with zip_file.open(filename) as file:
                content = io.TextIOWrapper(file, encoding="utf-8").read()
                atoms = ase.io.read(
                    io.StringIO(content), format="extxyz", index=slice(None)
                )  # reads multiple Atoms objects as frames if file contains trajectory
                if isinstance(atoms, Atoms):
                    atoms = [atoms]  # Wrap single Atoms object in a list
                if filename_to_info:
                    for atom in atoms:
                        atom.info["filename"] = filename
                atoms_list.extend(atoms)
    return atoms_list


def ase_atoms_to_zip(
    atoms_set: list[Atoms] | dict[str, Atoms], zip_filename: str | Path
) -> None:
    """Write ASE Atoms objects to a ZIP archive with each Atoms object as a separate
    extXYZ file, grouped by mat_id.

    Args:
        atoms_set (list[Atoms] | dict[str, Atoms]): Either a list of ASE Atoms objects
            (which should have a 'material_id' in their Atoms.info dictionary) or a
            dictionary mapping material IDs to Atoms objects.
        zip_filename (str | Path): Path to the ZIP file to write.
    """
    # Group atoms by mat_id to avoid overwriting files with the same name

    if isinstance(atoms_set, dict):
        atoms_dict = atoms_set
    else:
        atoms_dict = defaultdict(list)

        # If input is a list, get material ID from atoms.info falling back to formula if missing
        for atoms in atoms_set:
            mat_id = atoms.info.get(Key.mat_id, f"no-id-{atoms.get_chemical_formula()}")
            atoms_dict[mat_id] += [atoms]

    # Write grouped atoms to the ZIP archive
    with zipfile.ZipFile(
        zip_filename, mode="w", compression=zipfile.ZIP_DEFLATED
    ) as zip_file:
        for mat_id, atoms_or_list in tqdm(
            atoms_dict.items(), desc=f"Writing ASE Atoms to {zip_filename=}"
        ):
            buffer = io.StringIO()  # string buffer to write the extxyz content
            for atoms in (
                atoms_or_list if isinstance(atoms_or_list, list) else [atoms_or_list]
            ):
                ase.io.write(
                    buffer, atoms, format="extxyz", append=True, write_info=True
                )

            # Write the combined buffer content to the ZIP file
            zip_file.writestr(f"{mat_id}.extxyz", buffer.getvalue())


def download_file(file_path: str, url: str) -> None:
    """Download the file from the given URL to the given file path.
    Prints rather than raises if the file cannot be downloaded.
    """
    file_dir = os.path.dirname(file_path)
    os.makedirs(file_dir, exist_ok=True)
    try:
        response = requests.get(url, timeout=5)

        response.raise_for_status()

        with open(file_path, "wb") as file:
            file.write(response.content)
    except requests.exceptions.RequestException:
        print(f"Error downloading {url=}\nto {file_path=}.\n{traceback.format_exc()}")


def maybe_auto_download_file(url: str, abs_path: str, label: str | None = None) -> None:
    """Download file if it doesn't exist and user confirms or auto-download is enabled."""
    if os.path.isfile(abs_path):
        return

    # whether to auto-download model prediction files without prompting
    auto_download_files = os.getenv("MBD_AUTO_DOWNLOAD_FILES", "true").lower() == "true"

    is_ipython = hasattr(builtins, "__IPYTHON__")
    # default to 'y' if auto-download is enabled or not in interactive session (TTY or iPython)
    answer = (
        "y"
        if auto_download_files or not (is_ipython or sys.stdin.isatty())
        else input(
            f"{abs_path!r} associated with {label=} does not exist. Download it "
            "now? This will cache the file for future use. [y/n] "
        )
    )
    if answer.lower().strip() == "y":
        print(f"Downloading {label!r} from {url!r} to {abs_path!r}")
        download_file(abs_path, url)


class MetaFiles(EnumMeta):
    """Metaclass of Files enum that adds base_dir and (member|label)_map class
    properties.
    """

    _base_dir: str

    def __new__(
        cls,
        name: str,
        bases: tuple[type, ...],
        namespace: _EnumDict,
        base_dir: str = DEFAULT_CACHE_DIR,
        **kwargs: Any,
    ) -> "MetaFiles":
        """Create new Files enum with given base directory."""
        obj = super().__new__(cls, name, bases, namespace, **kwargs)
        obj._base_dir = base_dir  # noqa: SLF001
        return obj

    @property
    def base_dir(cls) -> str:
        """Base directory of the file."""
        return cls._base_dir


class Files(StrEnum, metaclass=MetaFiles):
    """Enum of data files with associated file directories and URLs."""

    @property
    @abc.abstractmethod
    def url(self) -> str:
        """URL associated with the file."""

    @property
    def rel_path(self) -> str:
        """Path of the file relative to the repo's ROOT directory."""
        return self.value

    @property
    @abc.abstractmethod
    def label(self) -> str:
        """Label associated with the file."""

    @classmethod
    def from_label(cls, label: str) -> Self:
        """Get enum member from pretty label."""
        file = next((attr for attr in cls if attr.label == label), None)
        if file is None:
            import difflib

            similar_labels = difflib.get_close_matches(label, [k.label for k in cls])
            raise ValueError(
                f"{label=} not found in {cls.__name__}. Did you mean one of {similar_labels}?"
            )
        return file


class DataFiles(Files):
    """Enum of data files with associated file directories and URLs."""

    mp_computed_structure_entries = (
        "mp/2023-02-07-mp-computed-structure-entries.json.gz"
    )
    mp_elemental_ref_entries = "mp/2023-02-07-mp-elemental-reference-entries.json.gz"
    mp_energies = "mp/2023-01-10-mp-energies.csv.gz"
    mp_patched_phase_diagram = "mp/2023-02-07-ppd-mp.pkl.gz"
    mp_trj_json_gz = "mp/2022-09-16-mp-trj.json.gz"
    mp_trj_extxyz = "mp/2024-09-03-mp-trj.extxyz.zip"
    # snapshot of every task (calculation) in MP as of 2023-03-16 (14 GB)
    all_mp_tasks = "mp/2023-03-16-all-mp-tasks.zip"

    wbm_computed_structure_entries = (
        "wbm/2022-10-19-wbm-computed-structure-entries.json.bz2"
    )
    wbm_relaxed_atoms = "wbm/2024-08-04-wbm-relaxed-atoms.extxyz.zip"
    wbm_initial_structures = "wbm/2022-10-19-wbm-init-structs.json.bz2"
    wbm_initial_atoms = "wbm/2024-08-04-wbm-initial-atoms.extxyz.zip"
    wbm_cses_plus_init_structs = (
        "wbm/2022-10-19-wbm-computed-structure-entries+init-structs.json.bz2"
    )
    wbm_summary = "wbm/2023-12-13-wbm-summary.csv.gz"
    alignn_checkpoint = "2023-06-02-pbenner-best-alignn-model.pth.zip"
    phonondb_pbe_103_structures = (
        "phonons/2024-11-09-phononDB-PBE-103-structures.extxyz"
    )
    phonondb_pbe_103_kappa_no_nac = (
        "phonons/2024-11-09-kappas-phononDB-PBE-noNAC.json.gz"
    )
    wbm_dft_geo_opt_symprec_1e_2 = "data/wbm/dft-geo-opt-symprec=1e-2-moyo=0.3.1.csv.gz"
    wbm_dft_geo_opt_symprec_1e_5 = "data/wbm/dft-geo-opt-symprec=1e-5-moyo=0.3.1.csv.gz"

    @functools.cached_property
    def yaml(self) -> dict[str, dict[str, str]]:
        """YAML data associated with the file."""
        yaml_path = f"{PKG_DIR}/data-files.yml"

        with open(yaml_path) as file:
            yaml_data = yaml.safe_load(file)

        if self.name not in yaml_data:
            raise ValueError(f"{self.name=} not found in {yaml_path}")

        return yaml_data

    @property
    def url(self) -> str:
        """URL associated with the file."""
        url = self.yaml[self.name].get("url")
        if url is None:
            raise ValueError(f"{self.name!r} does not have a URL")
        return url

    @property
    def label(self) -> str:
        """No pretty label for DataFiles, use name instead."""
        return self.name

    @property
    def description(self) -> str:
        """Description associated with the file."""
        return self.yaml[self.name]["description"]

    @property
    def path(self) -> str:
        """File path associated with the file URL if it exists, otherwise
        download the file first, then return the path.
        """
        key, rel_path = self.name, self.rel_path

        if rel_path not in self.yaml[key]["path"]:
            raise ValueError(f"{rel_path=} does not match {self.yaml[key]['path']}")

        abs_path = f"{type(self).base_dir}/{rel_path}"
        if not os.path.isfile(abs_path):
            is_ipython = hasattr(builtins, "__IPYTHON__")
            # default to 'y' if not in interactive session, and user can't answer
            answer = (
                input(
                    f"{abs_path!r} associated with {key=} does not exist. Download it "
                    "now? This will cache the file for future use. [y/n] "
                )
                if is_ipython or sys.stdin.isatty()
                else "y"
            )
            if answer.lower().strip() == "y":
                print(f"Downloading {key!r} from {self.url} to {abs_path}")
                download_file(abs_path, self.url)
        return abs_path


df_wbm = pd.read_csv(DataFiles.wbm_summary.path)
# str() around Key.mat_id added for https://github.com/janosh/matbench-discovery/issues/81
df_wbm.index = df_wbm[str(Key.mat_id)]


# ruff: noqa: E501, ERA001 (ignore long lines in class Model)
class Model(Files, base_dir=f"{ROOT}/models"):
    """Data files provided by Matbench Discovery.
    See https://janosh.github.io/matbench-discovery/contribute for data descriptions.
    """

    alignn = "alignn/alignn.yml"
    # alignn_pretrained = "alignn/alignn.yml"
    # alignn_ff = "alignn/alignn-ff.yml"

    # BOWSR optimizer coupled with original megnet
    bowsr_megnet = "bowsr/bowsr.yml"

    # default CHGNet model from publication with 400,438 params
    chgnet = "chgnet/chgnet.yml"
    # chgnet_no_relax = None, "CHGNet No Relax"

    # CGCNN 10-member ensemble
    cgcnn = "cgcnn/cgcnn.yml"

    # CGCNN 10-member ensemble with 5-fold training set perturbations
    cgcnn_p = "cgcnn/cgcnn+p.yml"

    # DeepMD-DPA3 models
    dpa3_v1_mptrj = "deepmd/dpa3-v1-mptrj.yml"
    dpa3_v1_openlam = "deepmd/dpa3-v1-openlam.yml"

    # original M3GNet straight from publication, not re-trained
    m3gnet = "m3gnet/m3gnet.yml"
    # m3gnet_direct = None, "M3GNet DIRECT"
    # m3gnet_ms = None, "M3GNet MS"

    # MACE-MP-0 medium as published in https://arxiv.org/abs/2401.00096 trained on MPtrj
    mace_mp_0 = "mace/mace-mp-0.yml"
    mace_mpa_0 = "mace/mace-mpa-0.yml"  # trained on MPtrj and Alexandria

    # original MEGNet straight from publication, not re-trained
    megnet = "megnet/megnet.yml"

    # SevenNet trained on MPtrj
    sevennet_0 = "sevennet/sevennet-0.yml"
    sevennet_l3i5 = "sevennet/sevennet-l3i5.yml"

    # Magpie composition+Voronoi tessellation structure features + sklearn random forest
    voronoi_rf = "voronoi_rf/voronoi-rf.yml"

    # wrenformer 10-member ensemble
    wrenformer = "wrenformer/wrenformer.yml"

    # --- Proprietary Models
    # GNoME
    gnome = "gnome/gnome.yml"

    # MatterSim
    mattersim_v1_5m = "mattersim/mattersim-v1-5m.yml"

    # ORB
    orb = "orb/orb.yml"
    orb_mptrj = "orb/orb-mptrj.yml"

    # fairchem
    eqv2_s_dens = "eqV2/eqV2-s-dens-mp.yml"
    eqv2_m = "eqV2/eqV2-m-omat-mp-salex.yml"

    grace_2l_mptrj = "grace/grace-2L-mptrj.yml"
    grace_2l_oam = "grace/grace-2L-oam.yml"
    grace_1l_oam = "grace/grace-1L-oam.yml"

    # --- Model Combos
    # # CHGNet-relaxed structures fed into MEGNet for formation energy prediction
    # chgnet_megnet = "chgnet/2023-03-06-chgnet-0.2.0-wbm-IS2RE.csv.gz"
    # # M3GNet-relaxed structures fed into MEGNet for formation energy prediction
    # m3gnet_megnet = "m3gnet/2022-10-31-m3gnet-wbm-IS2RE.csv.gz"
    # megnet_rs2re = "megnet/2023-08-23-megnet-wbm-RS2RE.csv.gz"

    @functools.cached_property  # cache to avoid re-reading same file multiple times
    def metadata(self) -> dict[str, Any]:
        """Metadata associated with the model."""
        yaml_path = f"{type(self).base_dir}/{self.rel_path}"
        with open(yaml_path) as file:
            data = yaml.safe_load(file)

        if not isinstance(data, dict):
            raise TypeError(f"{yaml_path!r} does not contain valid YAML metadata")

        return data

    @property
    def label(self) -> str:
        """Pretty label associated with the model."""
        return self.metadata["model_name"]

    @property
    def url(self) -> str:
        """Pull request URL in which the model was originally added to the repo."""
        return self.metadata["pr_url"]

    @property
    def key(self) -> str:
        """Key associated with the file URL."""
        return self.metadata["model_key"]

    @property
    def metrics(self) -> dict[str, Any]:
        """Metrics associated with the model."""
        return self.metadata.get("metrics", {})

    @property
    def yaml_path(self) -> str:
        """YAML file path associated with the model."""
        return f"{type(self).base_dir}/{self.rel_path}"

    @property
    def discovery_path(self) -> str:
        """Prediction file path associated with the model."""
        rel_path = self.metrics.get("discovery", {}).get("pred_file")
        file_url = self.metrics.get("discovery", {}).get("pred_file_url")
        if not rel_path:
            raise ValueError(
                f"metrics.discovery.pred_file not found in {self.rel_path!r}"
            )
        abs_path = f"{ROOT}/{rel_path}"
        maybe_auto_download_file(file_url, abs_path, label=self.label)
        return abs_path

    @property
    def geo_opt_path(self) -> str | None:
        """File path associated with the file URL if it exists, otherwise
        download the file first, then return the path.
        """
        geo_opt_metrics = self.metrics.get("geo_opt", {})
        if geo_opt_metrics in ("not available", "not applicable"):
            return None
        rel_path = geo_opt_metrics.get("pred_file")
        file_url = geo_opt_metrics.get("pred_file_url")
        if not rel_path:
            raise ValueError(
                f"metrics.geo_opt.pred_file not found in {self.rel_path!r}"
            )
        abs_path = f"{ROOT}/{rel_path}"
        maybe_auto_download_file(file_url, abs_path, label=self.label)
        return abs_path

    @property
    def kappa_103_path(self) -> str | None:
        """File path associated with the file URL if it exists, otherwise
        download the file first, then return the path.
        """
        phonons_metrics = self.metrics.get("phonons", {})
        if phonons_metrics in ("not available", "not applicable"):
            return None
        rel_path = phonons_metrics.get("kappa_103", {}).get("pred_file")
        file_url = phonons_metrics.get("kappa_103", {}).get("pred_file_url")
        if not rel_path:
            raise ValueError(
                f"metrics.phonons.kappa_103.pred_file not found in {self.rel_path!r}"
            )
        abs_path = f"{ROOT}/{rel_path}"
        maybe_auto_download_file(file_url, abs_path, label=self.label)
        return abs_path


# render model keys as labels in plotly axes and legends
px.defaults.labels |= {k.name: k.label for k in Model}


def load_df_wbm_with_preds(
    *,
    models: Sequence[str | Model] = (),
    pbar: bool = True,
    id_col: str = Key.mat_id,
    subset: pd.Index | Sequence[str] | Literal[TestSubset.uniq_protos] | None = None,
    max_error_threshold: float | None = 5.0,
    **kwargs: Any,
) -> pd.DataFrame:
    """Load WBM summary dataframe with model predictions from disk.

    Args:
        models (Sequence[str], optional): Model names must be keys of
            matbench_discovery.data.Model. Defaults to all models.
        pbar (bool, optional): Whether to show progress bar. Defaults to True.
        id_col (str, optional): Column to set as df.index. Defaults to "material_id".
        subset (pd.Index | Sequence[str] | 'uniq_protos' | None, optional):
            Subset of material IDs to keep. Defaults to None, which loads all materials.
            'uniq_protos' drops WBM structures with matching prototype in MP
            training set and duplicate prototypes in WBM test set (keeping only the most
            stable structure per prototype). This increases the 'OOD-ness' of WBM.
        max_error_threshold (float, optional): Maximum absolute error between predicted
            and DFT formation energies before a prediction is filtered out as
            unrealistic. Doing this filtering is acceptable as it could also be done by
            a practitioner doing a prospective discovery effort. Predictions exceeding
            this threshold will be ignored in all downstream calculations of metrics.
            Defaults to 5 eV/atom.
        **kwargs: Keyword arguments passed to glob_to_df().

    Raises:
        ValueError: On unknown model names.

    Returns:
        pd.DataFrame: WBM summary dataframe with model predictions.
    """
    valid_models = {model.name for model in Model}
    if models == ():
        models = tuple(valid_models)
    inv_label_map = {key.label: key.name for key in Model}
    # map pretty model names back to Model enum keys
    models = {inv_label_map.get(model, model) for model in models}
    if unknown_models := ", ".join(models - valid_models):
        raise ValueError(f"{unknown_models=}, expected subset of {valid_models}")

    model_name: str = ""
    from matbench_discovery.data import df_wbm

    df_out = df_wbm.copy()

    try:
        prog_bar = tqdm(models, disable=not pbar, desc="Loading preds")
        for model_name in prog_bar:
            prog_bar.set_postfix_str(model_name)

            # use getattr(name) in case model_name is already a Model enum
            model = Model[getattr(model_name, "name", model_name)]

            df_preds = glob_to_df(model.discovery_path, pbar=False, **kwargs)

            with open(model.yaml_path) as file:
                model_data = yaml.safe_load(file)

            pred_col = (
                model_data.get("metrics", {}).get("discovery", {}).get("pred_col")
            )
            if not pred_col:
                raise ValueError(
                    f"pred_col not specified for {model_name} in {model.yaml_path!r}"
                )

            if pred_col not in df_preds:
                raise ValueError(
                    f"{pred_col=} set in {model.yaml_path!r}:metrics.discovery.pred_col "
                    f"not found in {model.discovery_path}"
                )

            df_out[model.label] = df_preds.set_index(id_col)[pred_col]
            if max_error_threshold is not None:
                if max_error_threshold < 0:
                    raise ValueError("max_error_threshold must be a positive number")
                # Apply centralized model prediction cleaning criterion (see doc string)
                bad_mask = (
                    abs(df_out[model.label] - df_out[MbdKey.e_form_dft])
                ) > max_error_threshold
                df_out.loc[bad_mask, model.label] = pd.NA
                n_preds, n_bad = len(df_out[model.label].dropna()), sum(bad_mask)
                if n_bad > 0:
                    print(
                        f"{n_bad:,} of {n_preds:,} unrealistic preds for {model_name}"
                    )
    except Exception as exc:
        exc.add_note(f"Failed to load {model_name=}")
        raise

    if subset == TestSubset.uniq_protos:
        df_out = df_out.query(MbdKey.uniq_proto)
    elif subset is not None:
        df_out = df_out.loc[subset]

    return df_out

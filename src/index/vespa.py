import asyncio
import logging
from collections import defaultdict
from io import BytesIO
from pathlib import Path
from typing import (
    Annotated,
    Generator,
    Mapping,
    NewType,
    Optional,
    Sequence,
    Tuple,
    Union,
    cast,
)

from cloudpathlib import S3Path
from cpr_data_access.parser_models import ParserOutput, PDFTextBlock, VerticalFlipError
from pydantic import BaseModel, Field
from vespa.application import Vespa
from vespa.io import VespaResponse
import numpy as np

from src import config
from src.utils import filter_on_block_type


_LOGGER = logging.getLogger(__name__)
SchemaName = NewType("SchemaName", str)
DocumentID = NewType("DocumentID", str)
Coord = tuple[float, float]
TextCoords = Sequence[Coord]  # TODO: Could do better - look at data access change
SEARCH_WEIGHTS_SCHEMA = SchemaName("search_weights")
FAMILY_DOCUMENT_SCHEMA = SchemaName("family_document")
DOCUMENT_PASSAGE_SCHEMA = SchemaName("document_passage")
_SCHEMAS_TO_PROCESS = [
    SEARCH_WEIGHTS_SCHEMA,
    FAMILY_DOCUMENT_SCHEMA,
    DOCUMENT_PASSAGE_SCHEMA,
]
# TODO: no need to parameterise now, but namespaces
# may be useful for some data separation labels later
_NAMESPACE = "doc_search"


class VespaConfigError(config.ConfigError):
    pass


class VespaIndexError(config.ConfigError):
    pass


class VespaSearchWeights(BaseModel):
    """Weights to be applied to each ranking element in searches"""

    name_weight: float
    description_weight: float
    passage_weight: float


class VespaDocumentPassage(BaseModel):
    """Document passage representation for search"""

    search_weights_ref: str
    family_document_ref: str
    text_block: str
    text_block_id: str
    text_block_type: str
    text_block_page: Optional[Annotated[int, Field(ge=0)]]
    text_block_coords: Optional[TextCoords]
    text_embedding: Annotated[list[float], 768]


class VespaFamilyDocument(BaseModel):
    """Family-Document combined data useful for search"""

    search_weights_ref: str
    family_name: str
    family_name_index: str
    family_description: str
    family_description_index: str
    family_description_embedding: Annotated[
        list[float], 768
    ]  # TODO: not yet enforced by pydantic
    family_import_id: str
    family_slug: str
    family_publication_ts: str
    family_publication_year: int
    family_category: str
    family_geography: str
    family_source: str
    document_import_id: str
    document_slug: str
    document_languages: Sequence[str]
    document_md5_sum: Optional[str]
    document_content_type: Optional[str]
    document_cdn_object: Optional[str]
    document_source_url: Optional[str]


def get_document_generator(
    tasks: Sequence[ParserOutput],
    embedding_dir_as_path: Union[Path, S3Path],
) -> Generator[Tuple[SchemaName, DocumentID, dict], None, None]:
    """
    Get generator for documents to index.

    Documents to index are those containing text passages and their embeddings.

    :param namespace: the Vespa namespace into which these documents should be placed
    :param tasks: list of tasks from the embeddings generator
    :param embedding_dir_as_path: directory containing embeddings .npy files.
        These are named with IDs corresponding to the IDs in the tasks.
    :yield Generator[Tuple[SchemaName, DocumentID, dict], None, None]: generator of
        Vespa documents along with their schema and ID.
    """

    search_weights_id = DocumentID("default_weights")
    search_weights = VespaSearchWeights(
        name_weight=2.5,
        description_weight=2.0,
        passage_weight=1.0,
    )
    yield SEARCH_WEIGHTS_SCHEMA, search_weights_id, search_weights.dict()

    _LOGGER.info(
        "Filtering unwanted text block types.",
        extra={"props": {"BLOCKS_TO_FILTER": config.BLOCKS_TO_FILTER}},
    )
    tasks = filter_on_block_type(
        inputs=tasks, remove_block_types=config.BLOCKS_TO_FILTER
    )

    physical_document_count = 0
    for task in tasks:
        task_array_file_path = cast(Path, embedding_dir_as_path / f"{task.document_id}.npy")
        with open(task_array_file_path, "rb") as task_array_file_like:
            embeddings = np.load(BytesIO(task_array_file_like.read()))

        family_document_id = DocumentID(task.document_metadata.family_import_id)
        family_document = VespaFamilyDocument(
            search_weights_ref=f"id:{_NAMESPACE}:search_weights::{search_weights_id}",
            family_name=task.document_name,
            family_name_index=task.document_name,
            family_description=task.document_description,
            family_description_index=task.document_description,
            family_description_embedding=embeddings[0].tolist(),
            family_import_id=task.document_metadata.family_import_id,
            family_slug=task.document_metadata.family_slug,
            family_publication_ts=task.document_metadata.publication_ts.isoformat(),
            family_publication_year=task.document_metadata.publication_ts.year,
            family_category=task.document_metadata.category,
            family_geography=task.document_metadata.geography,
            family_source=task.document_metadata.source,
            document_import_id=task.document_id,
            document_slug=task.document_slug,
            document_languages=task.document_metadata.languages,
            document_md5_sum=task.document_md5_sum,
            document_content_type=task.document_content_type,
            document_cdn_object=task.document_cdn_object,
            document_source_url=task.document_metadata.source_url,
        )
        yield FAMILY_DOCUMENT_SCHEMA, family_document_id, family_document.dict()
        physical_document_count += 1
        if (physical_document_count % 50) == 0:
            _LOGGER.info(
                f"Document generator processing {physical_document_count} "
                "physical documents"
            )

        try:
            text_blocks = task.vertically_flip_text_block_coords().get_text_blocks()
        except VerticalFlipError:
            _LOGGER.exception(
                f"Error flipping text blocks for {task.document_id}, coordinates "
                "will be incorrect for displayed passages"
            )
            text_blocks = task.get_text_blocks()

        for document_passage_idx, (text_block, embedding) in enumerate(
            zip(text_blocks, embeddings[1:, :])
        ):
            fam_doc_ref = f"id:{_NAMESPACE}:family_document::{family_document_id}"
            search_weights_ref = f"id:{_NAMESPACE}:search_weights::{search_weights_id}"
            document_passage = VespaDocumentPassage(
                family_document_ref=fam_doc_ref,
                search_weights_ref=search_weights_ref,
                text_block="\n".join(text_block.text),
                text_block_id=text_block.text_block_id,
                text_block_type=str(text_block.type),
                text_block_page=(
                    text_block.page_number
                    if isinstance(text_block, PDFTextBlock)
                    else None
                ),
                text_block_coords=(
                    text_block.coords if isinstance(text_block, PDFTextBlock) else None
                ),
                text_embedding=embedding.tolist(),
            )
            document_psg_id = DocumentID(f"{task.document_id}.{document_passage_idx}")
            yield DOCUMENT_PASSAGE_SCHEMA, document_psg_id, document_passage.dict()

    _LOGGER.info(
        f"Document generator processed {physical_document_count} physical documents"
    )


def _get_vespa_instance() -> Vespa:
    """
    Creates a Vespa instance based on validated config values.

    :return Vespa: a Vespa instance to use for populating a new namespace.
    """
    # TODO: consider creating a pydantic config objects & allowing pydantic to
    # validate the config values we have/throw validation errors

    config_issues = []
    if not config.VESPA_INSTANCE_URL:
        config_issues.append(
            "Vespa instance URL must be configured using environment "
            "variable: 'VESPA_INSTANCE_URL'"
        )

    if not config.VESPA_KEY_LOCATION:
        config_issues.append(
            "Vespa key location must be configured using environment "
            "variable: 'VESPA_KEY_LOCATION'"
        )
    key_location = Path(config.VESPA_KEY_LOCATION)
    if not (key_location.exists() or key_location.is_file()):
        config_issues.append(
            "Configured key location does not exist or is not a file: "
            f"variable: '{config.VESPA_KEY_LOCATION}'"
        )

    if not config.VESPA_CERT_LOCATION:
        config_issues.append(
            "Vespa instance URL must be configured using environment "
            "variable: 'VESPA_CERT_LOCATION'"
        )
    cert_location = Path(config.VESPA_CERT_LOCATION)
    if not (cert_location.exists() or cert_location.is_file()):
        config_issues.append(
            "Configured cert location does not exist or is not a file: "
            f"variable: '{config.VESPA_CERT_LOCATION}'"
        )

    if config_issues:
        raise VespaConfigError(f"Vespa configuration issues found: {config_issues}")

    return Vespa(
        url=config.VESPA_INSTANCE_URL,
        key=str(key_location),
        cert=str(cert_location),
    )


async def _batch_ingest(vespa: Vespa, to_process: Mapping[SchemaName, list]):
    responses: list[VespaResponse] = []
    for schema in _SCHEMAS_TO_PROCESS:
        if documents := to_process[schema]:
            responses.extend(
                vespa.feed_batch(
                    batch=list(documents),
                    schema=str(schema),
                    namespace=_NAMESPACE,
                    asynchronous=True,
                    connections=50,
                    batch_size=1000,
                )
            )

    errors = [(r.status_code, r.json) for r in responses if r.status_code >= 300]
    if errors:
        _LOGGER.error(
            "Indexing Failed",
            extra={"props": {"error_responses": errors}},
        )
        raise VespaIndexError("Indexing Failed")


def populate_vespa(
    tasks: Sequence[ParserOutput],
    embedding_dir_as_path: Union[Path, S3Path],
) -> None:
    """
    Index documents into Opensearch.

    :param pdf_parser_output_dir: directory or S3 folder containing output JSON
        files from the PDF parser.
    :param embedding_dir: directory or S3 folder containing embeddings from the
        text2embeddings CLI.
    """
    vespa = _get_vespa_instance()

    document_generator = get_document_generator(
        tasks=tasks,
        embedding_dir_as_path=embedding_dir_as_path,
    )

    # Process documents into Vespa in sized groups (bulk ingest operates on documents
    # of a single schema)
    to_process: dict[SchemaName, list] = defaultdict(list)

    for schema, doc_id, fields in document_generator:
        to_process[schema].append(
            {
                "id": doc_id,
                "fields": fields,
            }
        )

        if len(to_process[FAMILY_DOCUMENT_SCHEMA]) >= config.VESPA_DOCUMENT_BATCH_SIZE:
            asyncio.run(_batch_ingest(vespa, to_process))
            to_process.clear()

    asyncio.run(_batch_ingest(vespa, to_process))

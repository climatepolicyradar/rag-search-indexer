from cloudpathlib import S3Path
from pathlib import Path
from typing import Any, Generator
from unittest.mock import Mock, patch

from cpr_data_access.parser_models import ParserOutput
import numpy as np

from src.index.vespa_ import (
    build_vespa_family_document,
    build_vespa_document_passage,
    get_document_generator,
    VespaDocumentPassage,
    VespaFamilyDocument,
    VespaSearchWeights,
    SEARCH_WEIGHTS_SCHEMA,
    FAMILY_DOCUMENT_SCHEMA,
    DOCUMENT_PASSAGE_SCHEMA,
)
from src.utils import read_npy_file
from tests.src.conftest import get_parser_output


def test_build_vespa_family_document():
    parser_output = get_parser_output(1,1)
    model = build_vespa_family_document(
        task=parser_output,
        embeddings=[np.array([-0.11900115,  0.17448892])],
        search_weights_ref="id:doc_search:weight::default",
    )
    VespaFamilyDocument.model_validate(model)


def test_build_vespa_document_passage():
    parser_output = get_parser_output(1,1)
    text_block = parser_output.pdf_data.text_blocks[0]
    model = build_vespa_document_passage(
        family_document_id="doc.1.1",
        search_weights_ref="id:doc_search:weight::default",
        text_block=text_block,
        embedding=np.array([-0.11900115,  0.17448892]),
    )
    VespaDocumentPassage.model_validate(model)


@patch("src.index.vespa_.read_npy_file")
def test_get_document_generator(mock_read_npy_file):
    """Assert that the vespa document generator works as expected."""
    mock_read_npy_file.return_value = read_npy_file(
        Path("tests/src/data/CCLW.executive.10002.4495.npy")
    )

    embedding_dir_as_path = Path("s3://path/to/embeddings")

    # An array of ParserOutputs, some belonging to the same family.
    paths = [
        Path(__file__).parent / "data" / "CCLW.executive.10002.4495.json",
        Path(__file__).parent / "data" / "CCLW.executive.10002.4496.json",
    ]

    generator = get_document_generator(paths, embedding_dir_as_path)

    vespa_family_document_ids = []
    vespa_document_passage_fam_refs = []
    for schema, id, data in generator:
        if schema == FAMILY_DOCUMENT_SCHEMA:
            vespa_family_document_ids.append(id)
        if schema == DOCUMENT_PASSAGE_SCHEMA:
            vespa_document_passage_fam_refs.append(data["family_document_ref"])

    # Check every family document id is unique and that there's one for each task
    assert len(set(vespa_family_document_ids)) == len(vespa_family_document_ids)
    assert len(vespa_family_document_ids) == len(paths)

    # Check that every family document is referenced by one passage
    # (this is as we had one text block for each family document)
    assert len(vespa_family_document_ids) == len(set(vespa_document_passage_fam_refs))
    for ref in vespa_document_passage_fam_refs:
        # A document passage id CCLW.executive.0.0.0 would take the form
        # 'id:doc_search:family_document::CCLW.executive.0.0'
        ref_id_format = ref.split(":")[-1]
        assert ref_id_format in vespa_family_document_ids


@patch("src.index.vespa_.read_npy_file")
def test_get_document_generator_(
    mock_np_load: Mock,
    test_document_data: tuple[ParserOutput, Any],
    embeddings_dir_as_path: S3Path,
) -> None:
    """Test that the get_document_generator correctly yields data."""
    parser_output_path, embeddings = test_document_data
    
    parser_output = ParserOutput.model_validate_json(parser_output_path.read_text())
    # Test that the pdf data is valid to test against
    assert parser_output.pdf_data is not None

    parser_output_tb_pages = {
        block.page_number for block in parser_output.pdf_data.text_blocks
    }
    parser_output_md_pages = {
        page.page_number for page in parser_output.pdf_data.page_metadata
    }

    # All text block pages should exist in the page metadata object. Not all metadata
    # object pages should have a text block page as we may not have retrieved text for
    # every page.
    for text_block_page in parser_output_tb_pages:
        assert text_block_page in parser_output_md_pages

    mock_np_load.return_value = embeddings

    document_generator = get_document_generator(
        paths=[parser_output_path], embedding_dir_as_path=embeddings_dir_as_path
    )

    # Only loading one document so we know the order of schemas that should be
    # returned. This is helpful as we can strongly assert the yield structure.
    assert isinstance(document_generator, Generator)

    document = next(document_generator, None)
    assert document is not None

    schema, document_id, data = document
    assert schema == SEARCH_WEIGHTS_SCHEMA
    # TODO Can we get a list of options for this?
    assert document_id == "default_weights"
    assert isinstance(data, dict)
    VespaSearchWeights.model_validate(data)

    document = next(document_generator, None)
    assert document is not None

    schema, document_id, data = document
    assert schema == FAMILY_DOCUMENT_SCHEMA
    assert document_id == parser_output.document_metadata.import_id
    assert isinstance(data, dict)
    VespaFamilyDocument.model_validate(data)

    def validate_passages_and_collect_page_numbers(
        doc_gen: Generator, pages: set, counter: int
    ) -> set[int]:
        """
        A recursive function to iterate through the generator.

        The purpose is to validate the document passage content and collect the page
        numbers into set for testing.
        """
        document = next(doc_gen, None)
        if document is None:
            return pages
        assert document is not None

        schema, document_id, data = document  # type: ignore
        assert schema == DOCUMENT_PASSAGE_SCHEMA
        document_passage = VespaDocumentPassage.model_validate(data)

        id_page = document_passage.text_block_id.split("_")[1]

        assert document_id == parser_output.document_id + f".{counter}"
        assert document_passage.text_block_page == int(id_page)
        pages.add(document_passage.text_block_page)

        return validate_passages_and_collect_page_numbers(doc_gen, pages, counter + 1)

    document_pages = validate_passages_and_collect_page_numbers(
        document_generator, set(), 0
    )

    assert document_pages == parser_output_tb_pages
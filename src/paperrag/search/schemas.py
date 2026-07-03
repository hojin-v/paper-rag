from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)


class KeywordCandidate(BaseModel):
    keyword_id: int
    keyword: str
    similarity: float


class PaperSummary(BaseModel):
    paper_id: int
    title: str
    authors: str = ""
    published_year: int | None = None
    journal: str | None = None
    full_text_link: str | None = None
    keywords: list[str] = Field(default_factory=list)
    score: float
    reason: str


class SearchMatched(BaseModel):
    status: Literal["matched"] = "matched"
    matched_keyword: str
    match_type: Literal["exact", "selected"]
    result_id: str
    primary_paper: PaperSummary
    related_paper: PaperSummary | None = None


class SearchSuggest(BaseModel):
    status: Literal["suggest"] = "suggest"
    session_id: str
    candidates: list[KeywordCandidate] = Field(default_factory=list)


class SelectRequest(BaseModel):
    session_id: str
    keyword_id: int


class PaperInfo(BaseModel):
    paper_id: int
    title: str
    authors: str = ""
    published_year: int | None = None
    journal: str | None = None
    abstract_summary: str | None = None
    full_text_link: str | None = None
    keywords: list[str] = Field(default_factory=list)


class ParagraphInfo(BaseModel):
    paragraph_order: int
    section_name: str = ""
    original_text: str = ""
    cleaned_text: str = ""
    summary: str = ""
    keywords: list[str] = Field(default_factory=list)


class TableInfo(BaseModel):
    role: Literal["대표", "연관"]
    table_title: str | None = None
    table_text: str = ""
    table_summary: str | None = None


class ResultBundle(BaseModel):
    result_id: str
    query: str
    matched_keyword: str
    match_type: Literal["exact", "selected"]
    primary_paper: PaperSummary
    related_paper: PaperSummary | None = None
    primary_info: PaperInfo
    related_info: PaperInfo | None = None
    primary_paragraphs: list[ParagraphInfo] = Field(default_factory=list)
    related_paragraphs: list[ParagraphInfo] = Field(default_factory=list)
    tables: list[TableInfo] = Field(default_factory=list)
    created_at: datetime


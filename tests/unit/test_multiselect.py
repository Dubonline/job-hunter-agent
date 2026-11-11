import pytest

from job_hunter.browser.adapter_base import PageReadError
from job_hunter.browser.filters import validate_filters
from job_hunter.models.preferences import Search


def test_company_size_accepts_three_requested_options():
    validate_filters(Search(keywords=['Agent应用开发'], cities=['上海'],
                            company_size_filter=['20-99人', '100-499人', '500-999人']))


@pytest.mark.parametrize('values', [[], ['20-99人', '20-99人'], ['1000-9999人']])
def test_invalid_multiselect_fails_before_connection(values):
    with pytest.raises(PageReadError, match='M3_FILTER_NOT_READY'):
        validate_filters(Search(keywords=['Agent应用开发'], cities=['上海'], company_size_filter=values))

def test_parameterized_query_fixture():
    query = "SELECT name FROM users WHERE id = %s"
    params = (42,)
    assert "%s" in query
    assert params == (42,)

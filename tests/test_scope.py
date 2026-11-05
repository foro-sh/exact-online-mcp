import pytest

from exact_mcp import scope


@pytest.mark.parametrize(
    ("resource", "parts"),
    [
        ("crm/Addresses", ("crm", "Addresses")),
        (" /read/financial/AgingOverview", ("read/financial", "AgingOverview")),
        ("bulk/Financial/TransactionLines", ("bulk/Financial", "TransactionLines")),
        ("hrm/Costcenters", ("hrm", "Costcenters")),
    ],
)
def test_a_readable_resource_splits_into_service_and_entity(resource, parts):
    assert scope.resource_parts(resource) == parts


@pytest.mark.parametrize(
    ("resource", "message"),
    [
        ("payroll/Employees", "doesn't read payroll"),
        ("sync/Payroll/Employees", "doesn't read payroll"),
        ("hrm/LeaveRegistrations", "doesn't read hrm"),
        ("users/UserRoles", "doesn't read users"),
        ("sync/Deleted", "needs a service"),
        ("crm/Accounts?$select=*", "isn't a resource"),
        ("crm/Accounts(guid'x')", "isn't a resource"),
        ("../crm/Accounts", "isn't a resource"),
    ],
)
def test_an_unreadable_resource_says_why(resource, message):
    with pytest.raises(ValueError, match=message):
        scope.resource_parts(resource)


def test_fields_that_identify_people_are_refused_wherever_they_appear():
    # A refused field's name inside a quoted string is only a value.
    scope.check_fields(["Name", "Email"], "Name eq 'BirthDate'", "Name")

    for select, filter_, orderby in [
        (["Name", "BirthDate"], None, None),
        (["Name"], "startswith(SocialSecurityNumber, '1') eq true", None),
        (["Name"], None, "Creator desc"),
    ]:
        with pytest.raises(ValueError, match="identify people"):
            scope.check_fields(select, filter_, orderby)


@pytest.mark.parametrize("select", [["*"], ["Name,BirthDate"], ["Name BirthDate"]])
def test_each_select_entry_must_be_one_field_name(select):
    with pytest.raises(ValueError, match="one field name"):
        scope.check_fields(select)

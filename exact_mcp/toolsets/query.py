"""A generic read of the Exact resources no other tool covers, within the limits of `scope`."""

from typing import Annotated

from pydantic import Field

from .. import odata, scope
from ..tools import Division, Limit

Resource = Annotated[
    str, Field(description="An Exact REST resource, such as crm/Addresses or read/financial/AgingOverview.")
]


def register(tool, get_client) -> None:
    @tool
    async def describe_resource(resource: Resource, division: Division = None) -> list[dict]:
        """The fields of an Exact REST resource and their types, from Exact's $metadata, for use
        with query. Fields query may not read are left out."""
        service, entity = scope.resource_parts(resource)
        client = await get_client()
        properties = odata.entity_properties(await client.metadata(service, division), entity)
        return [prop for prop in properties if prop["name"].lower() not in scope.DENIED_FIELDS]

    @tool
    async def query(
        resource: Resource,
        select: Annotated[list[str], Field(min_length=1, description="Fields to return; see describe_resource.")],
        filter: Annotated[
            str | None, Field(description="An OData v2 $filter, such as \"substringof('x', Name) eq true\".")
        ] = None,
        orderby: Annotated[str | None, Field(description="An OData $orderby, such as 'Modified desc'.")] = None,
        limit: Limit = 25,
        division: Division = None,
    ) -> list[dict]:
        """Read any Exact REST resource that no other tool covers, with OData's $select, $filter
        and $orderby. Payroll, HR, user and mailbox resources, and fields that identify people or
        hold files, are off limits. Most resources page 60 rows a call and bulk/ ones 1,000, and
        Exact allows about 60 calls a minute."""
        service, entity = scope.resource_parts(resource)
        scope.check_fields(select, filter, orderby)
        client = await get_client()
        return await client.list(
            f"{service}/{entity}",
            select=[name.strip() for name in select],
            filters=[filter] if filter else [],
            orderby=orderby,
            limit=limit,
            division=division,
        )

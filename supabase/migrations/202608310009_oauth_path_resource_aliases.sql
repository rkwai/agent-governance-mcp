-- Allow one conservative path suffix on OAuth client resource aliases.
-- Additive only: do not edit 001-008.
--
-- Grok Bot stores one authorization per connector URL.  Distinct hostnames
-- already work as aliases of one client.  This CHECK also accepts
-- https://<same-host>/mcp/<slug> so one Railway process can present several
-- resource URLs without extra deploys or DNS.  Exact-match semantics are
-- unchanged: a token resource must equal the client's primary resource_url
-- or an active alias of that same client.  The primary anc_oauth_clients
-- resource_url remains /mcp; path is not a host.

ALTER TABLE public.anc_oauth_client_resource_aliases
    DROP CONSTRAINT IF EXISTS anc_oauth_client_resource_aliases_resource_url_check;

ALTER TABLE public.anc_oauth_client_resource_aliases
    ADD CONSTRAINT anc_oauth_client_resource_aliases_resource_url_check
    CHECK (resource_url ~ '^https://[^?#]+/mcp(?:/[a-z0-9]+(?:-[a-z0-9]+)*)?$');

COMMENT ON TABLE public.anc_oauth_client_resource_aliases IS
    'Additional exact protected-MCP-resource URLs for one allowlisted OAuth client, including same-origin /mcp/<slug> path aliases; the client primary resource_url remains valid.';

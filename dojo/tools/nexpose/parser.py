import re
from datetime import datetime

import html2text
from defusedxml import ElementTree
from django.conf import settings
from hyperlink._url import SCHEME_PORT_MAP

from dojo.models import Endpoint, Finding


class NexposeParser:

    """
    The objective of this class is to parse Nexpose's XML 2.0 Report.

    TODO: Handle errors.
    TODO: Test nexpose output version. Handle what happens if the parser doesn't support it.
    TODO: Test cases.

    @param xml_filepath A proper xml generated by nexpose
    """

    def get_scan_types(self):
        return ["Nexpose Scan"]

    def get_label_for_scan_types(self, scan_type):
        return scan_type  # no custom label for now

    def get_description_for_scan_types(self, scan_type):
        return "Use the full XML export template from Nexpose."

    def get_findings(self, xml_output, test):
        tree = ElementTree.parse(xml_output)
        vuln_definitions = self.get_vuln_definitions(tree)
        return self.get_items(tree, vuln_definitions, test)

    def parse_html_type(self, node):
        """
        Parse XML element of type HtmlType

        @return ret A string containing the parsed element
        """
        ret = ""
        tag = node.tag.lower()

        if tag == "containerblockelement":
            if len(list(node)) > 0:
                for child in list(node):
                    ret += self.parse_html_type(child)
            else:
                if node.text:
                    ret += "<div>" + str(node.text).strip()
                if node.tail:
                    ret += str(node.tail).strip() + "</div>"
                else:
                    ret += "</div>"
        if tag == "listitem":
            if len(list(node)) > 0:
                for child in list(node):
                    ret += self.parse_html_type(child)
            elif node.text:
                ret += "<li>" + str(node.text).strip() + "</li>"
        if tag == "orderedlist":
            i = 1
            for item in list(node):
                ret += (
                    "<ol>"
                    + str(i)
                    + " "
                    + self.parse_html_type(item)
                    + "</ol>"
                )
                i += 1
        if tag == "paragraph":
            if len(list(node)) > 0:
                for child in list(node):
                    ret += self.parse_html_type(child)
            else:
                if node.text:
                    ret += "<p>" + node.text.strip()
                if node.tail:
                    ret += str(node.tail).strip() + "</p>"
                else:
                    ret += "</p>"
        if tag == "unorderedlist":
            for item in list(node):
                unorderedlist = self.parse_html_type(item)
                if unorderedlist not in ret:
                    ret += "* " + unorderedlist
        if tag == "urllink":
            if node.text:
                ret += str(node.text).strip() + " "
            last = ""

            for attr in node.attrib:
                if last != "":
                    if node.get(attr) != node.get(last):
                        ret += str(node.get(attr)) + " "
                last = attr

        return ret

    def parse_tests_type(self, node, vulns_definitions):
        """
        Parse XML element of type TestsType

        @return vulns A list of vulnerabilities according to vulns_definitions
        """
        vulns = []

        for tests in node.findall("tests"):
            for test in tests.findall("test"):
                if test.get("id") in vulns_definitions and (
                    test.get("status")
                    in {
                        "vulnerable-exploited",
                        "vulnerable-version",
                        "vulnerable-potential",
                    }
                ):
                    vuln = vulns_definitions[test.get("id").lower()]
                    for desc in list(test):
                        if "pluginOutput" in vuln:
                            vuln[
                                "pluginOutput"
                            ] += "\n\n" + self.parse_html_type(desc)
                        else:
                            vuln["pluginOutput"] = self.parse_html_type(desc)
                    if settings.USE_FIRST_SEEN and (date := test.get("vulnerable-since")):
                        date = datetime.fromisoformat(date)
                        # It would be nice to be able to define it per Endpoint_Status but for now, we use the oldest known information
                        if not vuln.get("vulnerableSince") or (date < vuln["vulnerableSince"]):
                            vuln["vulnerableSince"] = date
                        else:
                            vuln["vulnerableSince"] = None
                    else:
                        vuln["vulnerableSince"] = None
                    vulns.append(vuln)

        return vulns

    def get_vuln_definitions(self, tree):
        """@returns vulns A dict of Vulnerability Definitions"""
        vulns = {}
        url_index = 0
        for vulnsDef in tree.findall("VulnerabilityDefinitions"):
            for vulnDef in vulnsDef.findall("vulnerability"):
                vid = vulnDef.get("id").lower()
                severity_chk = int(vulnDef.get("severity"))
                if severity_chk >= 9:
                    sev = "Critical"
                elif severity_chk >= 7:
                    sev = "High"
                elif severity_chk >= 4:
                    sev = "Medium"
                elif 0 < severity_chk < 4:
                    sev = "Low"
                else:
                    sev = "Info"
                vuln = {
                    "desc": "",
                    "name": vulnDef.get("title"),
                    "vector": vulnDef.get("cvssVector"),  # this is CVSS v2
                    "refs": {},
                    "resolution": "",
                    "severity": sev,
                    "tags": [],
                }
                for item in list(vulnDef):
                    if item.tag == "description":
                        for htmlType in list(item):
                            vuln["desc"] += self.parse_html_type(htmlType)

                    elif item.tag == "exploits":
                        for exploit in list(item):
                            vuln["refs"][exploit.get("title")] = (
                                str(exploit.get("title")).strip()
                                + " "
                                + str(exploit.get("link")).strip()
                            )

                    elif item.tag == "references":
                        for ref in list(item):
                            if "URL" in ref.get("source"):
                                vuln["refs"][
                                    ref.get("source") + str(url_index)
                                ] = str(ref.text).strip()
                                url_index += 1
                            else:
                                vuln["refs"][ref.get("source")] = str(
                                    ref.text,
                                ).strip()

                    elif item.tag == "solution":
                        for htmlType in list(item):
                            vuln["resolution"] += self.parse_html_type(
                                htmlType,
                            )

                    # there is currently no method to register tags in vulns
                    elif item.tag == "tags":
                        for tag in list(item):
                            vuln["tags"].append(tag.text.lower())

                vulns[vid] = vuln
        return vulns

    def get_items(self, tree, vulns, test):
        hosts = []
        for nodes in tree.findall("nodes"):
            for node in nodes.findall("node"):
                host = {}
                host["name"] = node.get("address")
                host["hostnames"] = set()
                host["os"] = ""
                host["services"] = []
                host["vulns"] = self.parse_tests_type(node, vulns)

                host["vulns"].append(
                    {
                        "name": "Host Up",
                        "desc": "Host is up because it replied on ICMP request or some TCP/UDP port is up",
                        "severity": "Info",
                    },
                )

                for names in node.findall("names"):
                    for name in names.findall("name"):
                        host["hostnames"].add(name.text)

                for endpoints in node.findall("endpoints"):
                    for endpoint in endpoints.findall("endpoint"):
                        svc = {
                            "protocol": endpoint.get("protocol"),
                            "port": int(endpoint.get("port")),
                            "status": endpoint.get("status"),
                        }
                        for services in endpoint.findall("services"):
                            for service in services.findall("service"):
                                svc["name"] = service.get("name", "").lower()
                                svc["vulns"] = self.parse_tests_type(
                                    service, vulns,
                                )

                                for configs in service.findall(
                                    "configurations",
                                ):
                                    for config in configs.findall("config"):
                                        if "banner" in config.get("name"):
                                            svc["version"] = config.get("name")

                                svc["vulns"].append(
                                    {
                                        "name": "Open port {}/{}".format(
                                            svc["protocol"].upper(),
                                            svc["port"],
                                        ),
                                        "desc": '{}/{} port is open with "{}" service'.format(
                                            svc["protocol"],
                                            svc["port"],
                                            service.get("name"),
                                        ),
                                        "severity": "Info",
                                        "tags": [
                                            re.sub(
                                                r"[^A-Za-z0-9]+",
                                                "-",
                                                service.get("name").lower(),
                                            ).rstrip("-"),
                                        ]
                                        if service.get("name") != "<unknown>"
                                        else [],
                                    },
                                )

                        host["services"].append(svc)

                hosts.append(host)

        dupes = {}

        for host in hosts:
            # manage findings by node only
            for vuln in host["vulns"]:
                dupe_key = vuln["severity"] + vuln["name"]

                find = self.findings(dupe_key, dupes, vuln)

                endpoint = Endpoint(host=host["name"])
                find.unsaved_endpoints.append(endpoint)
                find.unsaved_tags = vuln.get("tags", [])

            # manage findings by service
            for service in host["services"]:
                for vuln in service["vulns"]:
                    dupe_key = vuln["severity"] + vuln["name"]

                    find = self.findings(dupe_key, dupes, vuln)

                    endpoint = Endpoint(
                        host=host["name"],
                        port=service["port"],
                        protocol=service["name"]
                        if service["name"] in SCHEME_PORT_MAP
                        else service["protocol"],
                        fragment=service["protocol"].lower()
                        if service["name"] == "dns"
                        else None,
                        # A little dirty hack but in case of DNS it is
                        # important to know if vulnerability is on TCP or UDP
                    )
                    find.unsaved_endpoints.append(endpoint)
                    find.unsaved_tags = vuln.get("tags", [])

        return list(dupes.values())

    @staticmethod
    def findings(dupe_key, dupes, vuln):
        """ """
        if dupe_key in dupes:
            find = dupes[dupe_key]
            dupe_text = html2text.html2text(vuln.get("pluginOutput", ""))
            if dupe_text not in find.description:
                find.description += "\n\n" + dupe_text
        else:
            find = Finding(
                title=vuln["name"],
                description=html2text.html2text(vuln["desc"].strip())
                + "\n\n"
                + html2text.html2text(vuln.get("pluginOutput", "").strip()),
                severity=vuln["severity"],
                mitigation=html2text.html2text(vuln.get("resolution"))
                if vuln.get("resolution")
                else None,
                impact=vuln.get("vector") or None,
                false_p=False,
                duplicate=False,
                out_of_scope=False,
                dynamic_finding=True,
                date=vuln.get("vulnerableSince"),
            )
            # build references
            refs = ""
            for ref in vuln.get("refs", {}):
                if ref.startswith("BID"):
                    refs += f" * [{vuln['refs'][ref]}](https://www.securityfocus.com/bid/{vuln['refs'][ref]})"
                elif ref.startswith("CA"):
                    refs += f" * [{vuln['refs'][ref]}](https://www.cert.org/advisories/{vuln['refs'][ref]}.html)"
                elif ref.startswith("CERT-VN"):
                    refs += f" * [{vuln['refs'][ref]}](https://www.kb.cert.org/vuls/id/{vuln['refs'][ref]}.html)"
                elif ref.startswith("CVE"):
                    refs += f" * [{vuln['refs'][ref]}](https://cve.mitre.org/cgi-bin/cvename.cgi?name={vuln['refs'][ref]})"
                elif ref.startswith("DEBIAN"):
                    refs += f" * [{vuln['refs'][ref]}](https://security-tracker.debian.org/tracker/{vuln['refs'][ref]})"
                elif ref.startswith("XF"):
                    refs += f" * [{vuln['refs'][ref]}](https://exchange.xforce.ibmcloud.com/vulnerabilities/{vuln['refs'][ref]})"
                elif ref.startswith("URL"):
                    refs += f" * URL: {vuln['refs'][ref]}"
                else:
                    refs += f" * {ref}: {vuln['refs'][ref]}"
                refs += "\n"
            find.references = refs
            # update CVE
            if "CVE" in vuln.get("refs", {}):
                find.unsaved_vulnerability_ids = [vuln["refs"]["CVE"]]
            find.unsaved_endpoints = []
            dupes[dupe_key] = find
        return find

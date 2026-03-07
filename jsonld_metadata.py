import rdflib
from rdflib import Namespace

SCENARIOS = Namespace("https://secorolab.github.io/metamodels/scenarios/osc/")
ROBOVAST = Namespace("https://purl.org/robovast/metamodels/")
MAP_METADATA = Namespace("https://purl.org/secorolab/metamodels/environment#")
DATASET_IRI = "https://purl.org/robovast/datasets/2026-03-07-robovast-navigation/"

DATASET = Namespace(DATASET_IRI)

def load_graph(file_path):
    g = rdflib.Graph()
    g.parse(file_path, format="json-ld")
    # g.bind("dataset", "https://purl.org/robovast/datasets/results")
    g.bind("robovast", ROBOVAST)
    g.bind("scenarios", SCENARIOS)
    g.bind("env", MAP_METADATA)
    g.bind("dataset", DATASET)
    return g


if __name__ == '__main__':
    import os
    import yaml
    import glob
    from rdflib import PROV, DCTERMS, Namespace, DCMITYPE
    from pyld import jsonld
    import json
    import subprocess
    from rdflib.tools.rdf2dot import rdf2dot

    ID = "@id"
    CONTEXT = "@context"
    TYPE = "@type"
    iri_context = {
        CONTEXT: {
        "agn": "https://secorolab.github.io/metamodels/agent#",
        "smm": "https://secorolab.github.io/metamodels/scenarios#",
        "env": "https://secorolab.github.io/metamodels/environment#",
        "xsd": "http://www.w3.org/2001/XMLSchema#",
        "prov": "http://www.w3.org/ns/prov#",
        "dct": "http://purl.org/dc/terms/",
        "dataset": "https://purl.org/robovast/datasets/2026-03-07-robovast-navigation/"
        # "@base": "https://purl.org/robovast/datasets/2026-robovast-navigation/",
        }
    }

    base_context = {
        CONTEXT: [
            "https://secorolab.github.io/metamodels/prov.json"
        ]
    }

    campaigns = glob.glob("results/*/")

    graph = []

    for campaign in campaigns:
        with open(os.path.join(campaign, "metadata.yaml"), "r") as f:
            metadata = yaml.safe_load(f)

        graph.append({
            ID: "https://purl.org/robovast/",
            TYPE: PROV["SoftwareAgent"],
            DCTERMS["hasVersion"] : metadata["execution"]["robovast_version"]
        })
        graph.append(
            {
                ID: "https://purl.org/secorolab/scenery_builder/",
                TYPE: PROV["SoftwareAgent"],
                DCTERMS["hasVersion"] : metadata["execution"].get("scenery_builder_version")
            }
        )

        campaign_activity = {
            ID: DATASET[campaign+"execution/"],
            TYPE: [PROV["Activity"], ROBOVAST[metadata["execution"]["execution_type"].capitalize()]],
            "started_at": metadata["execution"]["execution_time"],
            "wasAssociatedWith": "https://purl.org/robovast/",
            ROBOVAST["runs"]: metadata["execution"]["runs"]
        }
        graph.append(campaign_activity)

        campaign_entity = {
            ID: DATASET[campaign],
            TYPE: PROV["Entity"],
            "wasGeneratedBy": campaign_activity[ID]
        }
        graph.append(campaign_entity)



        CAMPAIGN = Namespace(f"{DATASET_IRI}{campaign}")

        robot_files = [CAMPAIGN[l] for l in metadata["run_files"]]

        # TODO This is very hardcoded for the fair_metadata.json file
        agent = metadata.pop("metadata")
        agent[ID] = CAMPAIGN["turtlebot4"] # TODO Fix this
        agent[TYPE] = PROV["Agent"]
        agent["atLocation"] = robot_files
        # We add the agent at the end once eit's associated with all the runs

        for robot_file in robot_files:
            graph.append(
            {
                ID: robot_file, 
                TYPE: PROV["Location"],
            }
            )

        abstract_scenario = {
                ID: CAMPAIGN[f"_config/scenario.osc"],
                TYPE: [PROV["Entity"], SCENARIOS["AbstractScenario"]],
        }
        graph.append(abstract_scenario)

        vast_config = {
            ID: CAMPAIGN["_config/most_important.vast"],
            TYPE: [PROV["Entity"], ROBOVAST["VastConfiguration"]],
            "references": abstract_scenario[ID]
        }
        graph.append(vast_config)

        gen_activity = {
            ID: DATASET[campaign+"config_generation"],
            TYPE: [PROV["Activity"]],
            "used": [vast_config[ID], abstract_scenario[ID]],
            "wasInfluencedBy": campaign_activity[ID]
        }
        graph.append(gen_activity)

        for config in glob.glob(os.path.join(campaign, "_config", "*")):
            print(config)


        for e in glob.glob(os.path.join(campaign, "_execution", "*")):
            print(e)

        for t in glob.glob(os.path.join(campaign, "_transient", "*")):
            print(t)

        print()

        all_configs = {c["name"]: c for c in metadata["configurations"]}

        configs = glob.glob(os.path.join(campaign, "*/"))
        # TODO Remove the [:1] when ready to process all configs
        for config_path in sorted(configs[:1]):
            CONFIG = Namespace(f"{DATASET_IRI}{config_path}")
            config = os.path.split(os.path.split(config_path)[0])[-1]
            if config == "_config":
                continue
            elif config == "_execution":
                continue
            elif config == "_transient":
                continue
            
            config_md = all_configs[config]

            n = {
                ID: CAMPAIGN[config_path],
                TYPE: [PROV["Entity"], SCENARIOS["ConcreteScenario"]],
                "wasGeneratedBy": gen_activity[ID],
                "references": CAMPAIGN[config_md["variations"][0]["fpm_file"]]
            }

            # TODO Only adding counts instead of other nodes. Do we want this?
            if isinstance(config_md["config"]["goal_pose"], dict):
                n[ROBOVAST["goals"]] = 1
            else:
                n[ROBOVAST["goals"]] = len(config_md["goal_pose"])
            n[ROBOVAST["obstacles"]] = len(config_md["config"]["static_objects"])

            graph.append(n)

            fpm_activity = {
                ID: CAMPAIGN[config_path + "jsonld_generation/"],
                TYPE: PROV["Activity"],
                "used": CAMPAIGN[config_md["variations"][0]["fpm_file"]],
                "wasAssociatedWith": "https://purl.org/secorolab/scenery_builder",
                "wasInfluencedBy": gen_activity[ID]
            }

            graph.append(fpm_activity)

            json_files = [
                     CAMPAIGN[l]
                    for l in config_md["map_file"]["derived_from"]
                    if l.endswith(".json")
            ]
            
            jsonld_activity = {
                ID: CAMPAIGN[config + "artefact_generation/"],
                TYPE: PROV["Activity"],
                # Ignoring YAML files which contain the generation details
                "used": json_files,
                "wasAssociatedWith": "https://purl.org/secorolab/scenery_builder",
                "wasInfluencedBy": [gen_activity[ID], fpm_activity[ID]]
            }

            for j in json_files:
                graph.append(
                    {
                        ID: j,
                        TYPE: PROV["Entity"],
                        "wasGeneratedBy": fpm_activity[ID]
                    }
                )

            # Add mesh and stl
            graph.append(
                {
                    ID: CONFIG[config_md["config"]["map_file"]], 
                    TYPE: PROV["Entity"],
                    "wasGeneratedBy": jsonld_activity[ID],
                   MAP_METADATA["resolution"]: config_md["map_file"]["resolution"],
                   "references": CAMPAIGN[config_md["config"]["map_file"].replace("yaml", "pgm")],
                   # TODO created_at is empty
                    "generatedAt": config_md["map_file"]["updated_at"]
                }
            )
            graph.append(
                {
                    ID: CONFIG[config_md["config"]["mesh_file"]], 
                    TYPE: PROV["Entity"],
                    "wasGeneratedBy": jsonld_activity[ID],
                    "generatedAt": config_md["mesh_file"]["created_at"]
                }
            )

            # Activity for execution
            for run in config_md["test_results"]:
                platform = run["sysinfo"].pop("platform")
                sys_info = dict(**platform, **run["sysinfo"])
                sys_info = {ROBOVAST[k]: v for k, v in sys_info.items()}
                sys_info[ID] = CAMPAIGN[run["dir"]+"/sysinfo/"]

                run_activity = {
                    ID: CAMPAIGN[run["dir"]],
                    TYPE: PROV["Activity"],
                    "used": [
                    n[ID],
                    CONFIG[config_md["config"]["map_file"]], 
                    CONFIG[config_md["config"]["mesh_file"]]
                    ],
                    ROBOVAST["success"]: run["success"],
                    "startedAt": run["start_time"],
                    "endedAt": run["end_time"],
                    "wasAssociatedWith": "https://purl.org/robovast/",
                    ROBOVAST["sysinfo"]: sys_info[ID]
                }
                graph.append(run_activity)
                agent.setdefault("wasAssociatedWith",[]).append(run_activity[ID])

                for out_f in run["output_files"]:
                    # TODO Process the CSV files from the postprocessing metadata instead
                    if out_f.endswith("csv"):
                        continue
                    
                    graph.append(
                       {
                           ID: CAMPAIGN[out_f],
                           TYPE: PROV["Entity"],
                           "wasGeneratedBy": run_activity[ID]
                       }
                   ) 

                graph.append(sys_info)
        graph.append(agent)


        # For readability this compacts IRIs 
        # TODO Not fully working for some reason
        document = {"@graph": graph}
        document.update(iri_context)
        compact = jsonld.compact(document, base_context, {"expandContext": base_context, "graph": True})
        expanded = jsonld.expand(compact)
        flattened = jsonld.flatten(expanded)
        compact2 = jsonld.compact(flattened, iri_context, {"graph": True})

        file_name = "metadata"
        with open(f"{file_name}.prov.json", "w") as f:
            json.dump(compact2, f, indent=2)

        # Draw graph
        g = load_graph(f"{file_name}.prov.json")
        dot_file_path = f"{file_name}.dot"
        pdf_file_path = f"{file_name}.pdf"
        with open(dot_file_path, "w+") as dotfile:
            rdf2dot(g, dotfile)
        cmd = ["dot", "-Tpdf", dot_file_path, "-o", pdf_file_path]
        subprocess.Popen(cmd).communicate()
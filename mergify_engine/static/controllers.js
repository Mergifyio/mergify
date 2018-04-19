/* Controllers */

var app = angular.module('triage.controllers', ['classy']);

app.classy.controller({
    name: 'AppController',
});

app.classy.controller({
    name: 'PullsController',
    inject: ['$scope', '$http', '$interval', '$location', '$window', '$timeout', '$localForage', '$routeParams'],
    init: function() {
        'use strict';
        this.refresh_interval = 5 * 60;

        this.$scope.counter = 0;
        this.$scope.rq_default_count = -1;
        this.$scope.autorefresh = false;
	    this.$localForage.getItem('travis_token').then((token) => { this.$scope.travis_token = token ; });
        this.$scope.event = false;
        this.opened_travis_tabs = {};
        this.opened_commits_tabs = {};
        this.$scope.tabs_are_open = {};


        // NOTE(sileht): no event for now, it opens too many connections
        if(false && typeof(EventSource) !== "undefined") {
            console.log("event enabled");
            this.$scope.event = true;
            var source = new EventSource('/status/stream');
            source.addEventListener("ping", (event) => {
                // Just for testing the connection for heroku
            }, false);
            source.addEventListener("refresh", (event) => {
                this.update_pull_requests(JSON.parse(event.data));
                this.$scope.$apply()
            }, false);
            source.addEventListener("rq-refresh", (event) => {
                this.$scope.rq_default_count = event.data;
            }, false);
        } else {
            this.refresh();
            if (this.$location.search().autorefresh === "true"){
                console.log("auto refresh enabled");
                this.$scope.autorefresh = true;
                this.$interval(this.count, 1 * 1000);
                this.$interval(this.refresh, this.refresh_interval * 1000);
            }
        }
    },
    methods: {
        count: function(){
            this.$scope.counter -= 1;
        },
        refresh: function() {
            console.log("refreshing");
            this.$scope.refreshing = true;
            this.$http({'method': 'GET', 'url': '/status/' + this.$routeParams.installation_id}).then((response) => {
                this.update_pull_requests(response.data);
            }).error(this.on_error);
        },
        update_pull_requests: function(data) {
            var old_tabs = {"travis": this.opened_travis_tabs,
                            "commits": this.opened_commits_tabs}
            this.opened_travis_tabs = {};
            this.opened_commits_tabs = {};
            this.$scope.groups = []
            data.forEach((group) => {

                var repo;

                group.pulls.forEach((pull) => {
                    repo = pull.base.repo.full_name;
                    ["travis", "commits"].forEach((type) => {
                        var tabs = old_tabs[type];
                        if (tabs.hasOwnProperty(repo) && tabs[repo].includes(pull.number)) {
                            this.open_info(pull, type);
                        }
                    });

                    if (pull.mergify_engine_travis_state == "pending") {
                        this.refresh_travis(pull);
                    }
                });

                this.$scope.groups.push(group)
            });
            this.$scope.last_update = new Date();
            this.$scope.refreshing = false;
            this.$scope.counter = this.refresh_interval;
        },
        on_error: function(data, status) {
            console.warn(data, status);
            this.$scope.refreshing = false;
            this.$scope.counter = this.refresh_interval;
        },
        hide_all_tabs: function() {
            this.$scope.groups.forEach((group) => {
                group.pulls.forEach((pull) => {
                    this.close_info(pull, "travis");
                    this.close_info(pull, "commits");
                });
            });
        },
        toggle_info: function(pull, type) {
            var open = pull["open_" + type + "_row"];
            this.close_info(pull, "commits");
            this.close_info(pull, "travis");
            if (!open) {
                this.open_info(pull, type);

                if (type === "travis") {
                    if (["success", "failure", "error"].indexOf(pull.mergify_engine_travis_state) === -1) {
                        this.refresh_travis(pull);
                    }
                }
            }
        },
        open_info: function(pull, type) {
            var repo = pull.base.repo.full_name
            var tab = "opened_" + type + "_tabs";
            if (!this[tab].hasOwnProperty(repo)){
                this[tab][repo] = [];
            }
            this[tab][repo].push(pull.number)
            pull["open_" + type + "_row"] = true;
        },
        close_info: function(pull, type) {
            var repo = pull.base.repo.full_name
            var tab = "opened_" + type + "_tabs";
            if (!this[tab].hasOwnProperty(repo)){
                this[tab][repo] = [];
            }
            this[tab][repo] = this[tab][repo].filter(e => e !== pull.number);
            pull["open_" + type + "_row"] = false;
        },
        refresh_travis: function(pull) {
            if (!pull.mergify_engine_travis_detail)
                pull.mergify_engine_travis_detail = new Object()
            pull.mergify_engine_travis_detail.refreshing = true;

            var build_id = pull.mergify_engine_travis_url.split("?")[0].split("/").slice(-1)[0];
            var v2_headers = { "Accept": "application/vnd.travis-ci.2.1+json" };
            var travis_base_url = 'https://api.travis-ci.org';
            this.$http({
                "method": "GET",
                "url": travis_base_url + "/builds/" + build_id,
                "headers": v2_headers,
            }).then((response) => {

                var count_updated_job = 0;
                var build = response.data.build;
                build.resume_state = pull.mergify_engine_travis_state;
                build.jobs = [];
                build.refreshing = false;
                build.job_ids.forEach((job_id) => {
                    this.$http({
                        "method": "GET",
                        "url": travis_base_url + "/jobs/" + job_id,
                        "headers": v2_headers,
                    }).then((response) => {
                        if (pull.mergify_engine_travis_state == "pending" && response.data.job.state == "started") {
                            build.resume_state = "working";
                        }
                        build.jobs.push(response.data.job);
                        count_updated_job += 1;
                        if (count_updated_job == build.job_ids.length){
                            pull.mergify_engine_travis_detail = build;
                        }
                    });
                })
            });
        },
        open_all_commits: function(pull){
            pull.mergify_engine_commits.forEach((commit) => {
                var url = "https://github.com/" + pull.base.repo.full_name +
                    "/pull/" + pull.number + "/commits/" + commit.sha;
                this.$window.open(url, commit.sha);
            });
        },
        JobSorter: function(job){
            return parseInt(job.number.replace(".", ""));
        },
	save_travis_token(){
		this.$localForage.setItem('travis_token', this.$scope.travis_token);
	},
	restart_job: function(pull, job) {
            var v3_headers = { "Travis-API-Version": "3", "Authorization": "token " + this.$scope.travis_token };
            var travis_base_url = 'https://api.travis-ci.org';

	    this.$http({
		"method": "POST",
		"url": travis_base_url + "/job/" + job.id + "/restart",
		"headers": v3_headers,
	    }).then((response) => {
		job.restart_state = "ok";
		this.$timeout(() => { this.refresh_travis(pull); }, 1000);
	    }, (error) => {
		job.restart_state = "ko";
	    });
	}
    },
});

app.filter("GetCommitTitle", function(){
    return function(commit) {
        return commit.commit.message.split("\n")[0];
    }
});

app.filter("SelectLines", function(){
    return function(text, pos, len) {
        var lines = text.split("\n");
        lines =  lines.slice(pos-len, pos+len)
        if (lines.length <= 0){
            return text;
        } else {
            return lines.join("\n");
        }
    }
});

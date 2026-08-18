pipeline {
    agent any
    stages {
        stage('Echo') {
            steps {
                // A closing brace inside a string must not end a block:
                sh "echo }"
                sh 'echo { unbalanced in single quotes'
                sh """
                   heredoc with } and { inside
                """
                /* a block comment with } in it */
                script {
                    def matched = (env.BRANCH_NAME =~ /release\/.*/)
                    def ratio = 10 / 2
                    echo "matched=${matched} ratio=${ratio}"
                }
            }
        }
    }
}
